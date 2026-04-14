"""
AI 分析 API 端点

提供智能日志分析和链路分析的 REST API
支持基于规则的分析和 LLM 大模型分析

Date: 2026-02-09
"""

import asyncio
from collections import OrderedDict
import contextlib
from contextvars import ContextVar
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import Dict, Any, Optional, List, Tuple
from pydantic import BaseModel, Field
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone

from ai.analyzer import get_log_analyzer
from ai import followup_command as followup_command_utils
from ai.followup_command import (
    _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT,
    _assert_followup_command_is_suggested,
    _build_followup_exec_disabled_response,
    _is_truthy_env,
    _normalize_followup_command_line,
    _normalize_followup_command_match_key,
    _validate_requested_followup_command,
)
from ai.langchain_runtime import run_followup_langchain
from ai.llm_service import get_llm_service, reset_llm_service
from ai.knowledge_provider import get_knowledge_gateway, shutdown_knowledge_gateway, reload_knowledge_gateway
from ai.followup_prompt_helpers import (
    _build_followup_planner_prompt,
    _build_followup_reflection,
    _build_followup_response_instruction,
    _compact_conversation_for_prompt,
)
from ai.followup_planning_helpers import (
    _append_followup_react_summary,
    _build_followup_actions,
    _build_followup_react_loop,
    _build_followup_subgoals,
    _prioritize_followup_actions_with_react_memory,
)
from ai.followup_react_helpers import (
    _build_followup_react_memory,
    _merge_reflection_with_react_memory,
)
from ai.followup_session_helpers import (
    _build_followup_history,
    _ensure_followup_analysis_session,
    _seed_followup_runtime_history_session,
    _upsert_followup_user_message,
)
from ai.followup_runtime_helpers import (
    _build_followup_long_term_memory,
    _build_followup_runtime_thread_memory,
    _resolve_followup_answer_bundle,
)
from ai.followup_orchestration_helpers import (
    _emit_followup_event,
    _format_sse_event,
    _remaining_timeout,
    _run_followup_auto_exec_react_loop,
    _resolve_followup_timeout_profile,
)
from ai.followup_v2_adapter import run_followup_v2_adapter
from ai.agent_runtime import event_protocol, get_agent_runtime_service
from ai.agent_runtime.exec_client import (
    ExecServiceClientError,
    execute_command as execute_controlled_command,
    precheck_command as precheck_controlled_command,
)
from ai.agent_runtime.user_question_adapter import build_business_question
from ai.agent_runtime.status import is_terminal_run_status
from ai.followup_context_helpers import _build_context_pills, _build_followup_references
from ai.followup_command_spec import (
    build_command_spec_self_repair_payload,
    compile_followup_command_spec,
    map_followup_reason_group,
    normalize_followup_command_spec,
    normalize_followup_reason_code,
)
from ai.followup_persistence_helpers import (
    _persist_followup_messages_and_history,
    _update_followup_session_summary,
)
from ai.project_knowledge_pack import select_project_knowledge
from ai.kb_route_helpers import (
    _build_kb_search_request_context,
    _build_kb_search_response,
    _execute_kb_search,
    _raise_for_kb_runtime_warning,
    _require_kb_search_query,
    _resolve_kb_runtime_options_payload,
    _resolve_kb_search_effective_mode,
)
from ai.kb_draft_helpers import (
    _build_kb_from_analysis_response,
    _build_kb_merged_history_messages,
    _load_kb_analysis_session_payload,
    _require_kb_analysis_session_id,
    _resolve_kb_draft_bundle,
    _resolve_kb_draft_max_history_items,
    _resolve_kb_effective_save_mode,
)
from ai.kb_case_update_helpers import (
    _append_manual_remediation_change_history,
    _apply_manual_remediation_sync_result,
    _apply_remote_sync_result_to_case_metadata,
    _build_case_content_update_outcome,
    _build_case_content_update_response,
    _build_manual_remediation_change_summary,
    _build_manual_remediation_response,
    _prepare_case_content_update_metadata,
    _prepare_manual_remediation_case_update,
    _sync_case_update_with_remote,
    _sync_manual_remediation_update,
    _validate_manual_remediation_request,
)
from ai.kb_case_content_helpers import (
    _apply_case_content_request_fields,
    _require_editable_fields_for_case_content_update,
    _validate_case_content_required_fields,
)
from ai.history_route_helpers import (
    _build_ai_history_detail_response,
    _build_history_list_items,
    _build_history_list_response,
    _build_history_session_update_noop,
    _build_history_session_update_response,
    _collect_history_session_update_changes,
    _normalize_history_list_request,
)
from ai.followup_action_command_helpers import (
    _build_followup_action_payload,
    _load_followup_action_context,
    _load_followup_command_message_context,
    _merge_followup_action_into_context,
)
from ai.followup_action_draft_helpers import _build_followup_action_draft
from ai.case_query_helpers import (
    _build_case_detail_payload,
    _build_case_list_items,
    _resolve_case_detail_content_history,
)
from ai.case_history_helpers import (
    _build_case_content_change_summary,
    _case_store_append_change_history,
    _case_store_count_change_history,
    _case_store_list_change_history,
)
from ai.analysis_result_helpers import (
    _format_solution_text_standard,
    _normalize_analysis_result,
    _normalize_solutions,
    _normalize_solutions_from_text,
    _solutions_to_text,
)
from ai.json_dict_helpers import _parse_llm_json_dict
from ai.runtime_config_helpers import (
    _apply_kb_runtime_update,
    _apply_llm_runtime_update,
    _kb_provider_defaults,
    _normalize_kb_provider_name,
    _normalize_kb_runtime_config,
    _persist_kb_runtime_to_deployment_file,
    _persist_llm_runtime_to_deployment_file,
    _resolve_kb_deployment_file_path,
    _resolve_llm_deployment_file_path,
)
from ai.conversation_history_helpers import (
    _merge_conversation_history,
    _normalize_conversation_history,
    _session_messages_to_conversation_history,
    _trim_conversation_history,
)
from ai.request_flow_agent import get_request_flow_agent
from ai.session_history import (
    ALLOWED_SESSION_SORT_FIELDS,
    ALLOWED_SESSION_SORT_ORDERS,
    get_ai_session_store,
)
from storage.adapter import StorageAdapter

try:
    from prometheus_client import Counter
except Exception:  # pragma: no cover
    Counter = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

storage = None
_conversation_sessions: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

AI_FOLLOWUP_SESSION_CACHE_MAX = max(100, int(os.getenv("AI_FOLLOWUP_SESSION_CACHE_MAX", "1000")))
AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS = max(60, int(os.getenv("AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS", "3600")))

SUPPORTED_LLM_PROVIDERS = {"openai", "claude", "deepseek", "local"}
SUPPORTED_KB_REMOTE_PROVIDERS = {"ragflow", "generic_rest", "disabled"}
DEFAULT_LLM_DEPLOYMENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "ai-service.yaml")
)
DEFAULT_KB_DEPLOYMENT_FILE = DEFAULT_LLM_DEPLOYMENT_FILE
# 兼容旧测试/外部引用：继续从 api.ai 暴露这些常量。
_FOLLOWUP_COMMAND_BLOCKED_OPERATORS = followup_command_utils._FOLLOWUP_COMMAND_BLOCKED_OPERATORS
_FOLLOWUP_COMMAND_FENCE_PATTERN = followup_command_utils._FOLLOWUP_COMMAND_FENCE_PATTERN
_FOLLOWUP_COMMAND_INLINE_PATTERN = followup_command_utils._FOLLOWUP_COMMAND_INLINE_PATTERN
_FOLLOWUP_COMMAND_ALLOWED_HEADS = followup_command_utils._FOLLOWUP_COMMAND_ALLOWED_HEADS

_RUNTIME_V1_API_GUARD_BYPASS: ContextVar[bool] = ContextVar("runtime_v1_api_guard_bypass", default=False)

_RUNTIME_EVENT_VISIBILITY_DEFAULT = "default"
_RUNTIME_EVENT_VISIBILITY_DEBUG = "debug"
_RUNTIME_EVENT_VISIBLE_OPTIONS = {
    _RUNTIME_EVENT_VISIBILITY_DEFAULT,
    _RUNTIME_EVENT_VISIBILITY_DEBUG,
}
_RUNTIME_EVENT_DEFAULT_HIDDEN_TYPES = {
    "action_execution_retrying",
    "action_recovery_succeeded",
    "action_spec_validated",
}


def _runtime_v1_api_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_V1_API_ENABLED"), "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _ensure_runtime_v1_api_enabled() -> None:
    if _runtime_v1_api_enabled() or bool(_RUNTIME_V1_API_GUARD_BYPASS.get()):
        return
    raise HTTPException(
        status_code=410,
        detail={
            "code": "RUNTIME_V1_DISABLED",
            "message": "runtime v1 API is disabled; use /api/v2 thread-run APIs",
        },
    )


@contextlib.contextmanager
def runtime_v1_api_guard_bypass():
    token = _RUNTIME_V1_API_GUARD_BYPASS.set(True)
    try:
        yield
    finally:
        _RUNTIME_V1_API_GUARD_BYPASS.reset(token)


def _build_counter(name: str, description: str, *, labelnames: Optional[Tuple[str, ...]] = None):
    if Counter is None:
        return None
    try:
        if labelnames:
            return Counter(name, description, labelnames=labelnames)
        return Counter(name, description)
    except Exception:
        return None


def _metric_inc(counter_obj: Any, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
    if counter_obj is None:
        return
    try:
        if labels and hasattr(counter_obj, "labels"):
            counter_obj.labels(**labels).inc(amount)
            return
        counter_obj.inc(amount)
    except Exception:
        return


KB_MANUAL_REMEDIATION_UPDATE_TOTAL = _build_counter(
    "kb_manual_remediation_update_total",
    "Total successful manual remediation updates.",
)
AI_FOLLOWUP_REACT_REPLAN_TOTAL = _build_counter(
    "ai_followup_react_replan_total",
    "Total follow-up requests that require react replan.",
)
AI_FOLLOWUP_AUTO_EXEC_SUCCESS_TOTAL = _build_counter(
    "ai_followup_auto_exec_success_total",
    "Total successful readonly auto executions in follow-up react loop.",
)
AI_FOLLOWUP_AUTO_EXEC_FAILED_TOTAL = _build_counter(
    "ai_followup_auto_exec_failed_total",
    "Total failed/skipped readonly auto executions in follow-up react loop.",
)
AI_FOLLOWUP_REACT_MEMORY_HITS_TOTAL = _build_counter(
    "ai_followup_react_memory_hits_total",
    "Total react memory hits loaded for follow-up requests.",
)
AI_RUNTIME_COMMAND_ACTION_REQUEST_TOTAL = _build_counter(
    "ai_runtime_command_action_request_total",
    "Total runtime command action requests.",
)
AI_RUNTIME_STRUCTURED_SPEC_PRESENT_TOTAL = _build_counter(
    "ai_runtime_structured_spec_present_total",
    "Total runtime command action requests that carry a normalized command_spec.",
)
AI_RUNTIME_BLOCKED_MISSING_SPEC_TOTAL = _build_counter(
    "ai_runtime_blocked_missing_spec_total",
    "Total runtime command action requests blocked due to missing/invalid command_spec.",
)
AI_RUNTIME_SQL_PREFLIGHT_FAIL_TOTAL = _build_counter(
    "ai_runtime_sql_preflight_fail_total",
    "Total runtime command action requests blocked by SQL preflight failure.",
)
AI_RUNTIME_TIMEOUT_RETRY_TOTAL = _build_counter(
    "ai_runtime_timeout_retry_total",
    "Total runtime command retries triggered after timeout.",
)
AI_RUNTIME_TIMEOUT_RETRY_SUCCESS_TOTAL = _build_counter(
    "ai_runtime_timeout_retry_success_total",
    "Total runtime commands succeeded after at least one timeout retry.",
)
AI_FOLLOWUP_ACTION_SPEC_COMPILE_FAILED_TOTAL = _build_counter(
    "ai_followup_action_spec_compile_failed_total",
    "Total follow-up action spec compile failures by normalized reason.",
    labelnames=("reason", "reason_group"),
)
AI_FOLLOWUP_GLUE_REPAIR_SUCCESS_TOTAL = _build_counter(
    "ai_followup_glue_repair_success_total",
    "Total follow-up glue repair successes by normalized reason.",
    labelnames=("reason", "reason_group"),
)
AI_FOLLOWUP_NO_EXECUTABLE_QUERY_CANDIDATES_TOTAL = _build_counter(
    "ai_followup_no_executable_query_candidates_total",
    "Total follow-up rounds with no executable query candidates.",
)
AI_FOLLOWUP_SEMANTIC_INCOMPLETE_TOTAL = _build_counter(
    "ai_followup_semantic_incomplete_total",
    "Total follow-up semantic incomplete observations by normalized reason.",
    labelnames=("reason", "reason_group"),
)
_FOLLOWUP_SPEC_COMPILE_FAILURE_REASON_CODES = {
    "missing_or_invalid_command_spec",
    "missing_structured_spec",
    "missing_target_identity",
    "target_kind_mismatch",
    "target_identity_mismatch",
    "missing_namespace_for_k8s_clickhouse_query",
    "missing_pod_name_for_k8s_clickhouse_query",
    "pod_name_resolution_failed",
    "glued_command_tokens",
    "glued_sql_tokens",
    "invalid_kubectl_token",
    "suspicious_selector_namespace_glue",
    "unsupported_command_head",
    "unsupported_clickhouse_readonly_query",
    "pod_selector_requires_shell",
    "clickhouse_multi_statement_not_allowed",
    "answer_command_requires_structured_action",
}
_FOLLOWUP_GLUE_REASON_CODES = {
    "glued_command_tokens",
    "glued_sql_tokens",
    "invalid_kubectl_token",
    "suspicious_selector_namespace_glue",
}


def set_storage_adapter(storage_adapter: StorageAdapter):
    """设置 storage adapter"""
    global storage
    storage = storage_adapter
    try:
        from ai.runtime_v4.langgraph.checkpoint import set_graph_checkpoint_storage
        from ai.runtime_v4.targets import ensure_runtime_v4_default_targets, set_runtime_v4_target_storage
        from ai.similar_cases import get_case_store
        get_case_store(storage_adapter)
        gateway = get_knowledge_gateway(storage_adapter)
        gateway.start_outbox_worker()
        get_ai_session_store(storage_adapter)
        get_agent_runtime_service(storage_adapter)
        target_registry = set_runtime_v4_target_storage(storage_adapter)
        seed_result = ensure_runtime_v4_default_targets(target_registry)
        if bool(seed_result.get("enabled")):
            logger.info(
                "runtime v4 target defaults ensured: created=%s updated=%s skipped=%s",
                len(seed_result.get("created") or []),
                len(seed_result.get("updated") or []),
                len(seed_result.get("skipped") or []),
            )
        set_graph_checkpoint_storage(storage_adapter)
    except Exception as e:
        logger.warning(f"Failed to initialize AI stores with storage adapter: {e}")


def shutdown_background_tasks() -> None:
    """关闭后台任务（Outbox worker 等）。"""
    try:
        shutdown_knowledge_gateway()
    except Exception as e:
        logger.warning(f"Failed to shutdown AI background tasks cleanly: {e}")
    try:
        get_agent_runtime_service().shutdown()
    except Exception as e:
        logger.warning(f"Failed to shutdown AI runtime cleanly: {e}")


class AIRuntimeOptionsRequest(BaseModel):
    """AI runtime options."""

    use_llm: bool = True
    max_iterations: int = 4
    auto_exec_readonly: bool = True


class AIRunCreateRequest(BaseModel):
    """AI run creation request."""

    session_id: str = ""
    question: str
    analysis_context: Dict[str, Any] = Field(default_factory=dict)
    runtime_options: Dict[str, Any] = Field(default_factory=dict)


class AIRunCancelRequest(BaseModel):
    """AI run cancellation request."""

    reason: str = "user_cancelled"


class AIRunInterruptRequest(BaseModel):
    """AI run interrupt request (Esc semantic)."""

    reason: str = "user_interrupt_esc"


class AIRunApproveRequest(BaseModel):
    """AI run approval resolution request."""

    approval_id: str = ""
    decision: str = "approved"
    comment: str = ""
    confirmed: bool = True
    elevated: bool = False


class AIRunInputRequest(BaseModel):
    """User input request for waiting_user_input state."""

    text: str
    source: str = "user"


class AIRunCommandRequest(BaseModel):
    """AI runtime command execution request."""

    action_id: str = ""
    step_id: str = ""
    command: str = ""
    command_spec: Dict[str, Any] = Field(default_factory=dict)
    diagnosis_contract: Dict[str, Any] = Field(default_factory=dict)
    purpose: str
    title: str = ""
    tool_name: str = "command.exec"
    confirmed: bool = False
    elevated: bool = False
    approval_token: str = ""
    client_deadline_ms: int = 0
    timeout_seconds: int = 20


_AI_RUNTIME_LAB_PROFILE = "ai_runtime_lab"


def _normalize_text_fingerprint(value: Any) -> str:
    text = _as_str(value).strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[`'\"，。！？、,.!?;:()\\[\\]{}<>]+", "", text)
    return text.strip()


def _is_ai_runtime_lab_mode(
    *,
    analysis_context: Optional[Dict[str, Any]],
    runtime_options: Optional[Dict[str, Any]] = None,
) -> bool:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    safe_runtime_options = runtime_options if isinstance(runtime_options, dict) else {}
    profile = _as_str(
        safe_context.get("runtime_profile")
        or safe_runtime_options.get("runtime_profile")
    ).strip().lower()
    return profile == _AI_RUNTIME_LAB_PROFILE


def _extract_last_assistant_after_user(
    history: List[Dict[str, Any]],
    start_index: int,
) -> Dict[str, Any]:
    latest_assistant: Dict[str, Any] = {}
    for index in range(start_index + 1, len(history)):
        item = history[index] if isinstance(history[index], dict) else {}
        role = _as_str(item.get("role")).strip().lower()
        if role == "user":
            break
        if role == "assistant" and _as_str(item.get("content")).strip():
            latest_assistant = item
    return latest_assistant


def _find_duplicate_question_turn(
    *,
    history: List[Dict[str, Any]],
    question: str,
) -> Dict[str, Any]:
    normalized_question = _normalize_text_fingerprint(question)
    if not normalized_question:
        return {}
    latest_match: Dict[str, Any] = {}
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        role = _as_str(item.get("role")).strip().lower()
        if role != "user":
            continue
        normalized_content = _normalize_text_fingerprint(item.get("content"))
        if not normalized_content or normalized_content != normalized_question:
            continue
        assistant_message = _extract_last_assistant_after_user(history, index)
        if assistant_message:
            latest_match = {
                "user_message": item,
                "assistant_message": assistant_message,
            }
    return latest_match


def _normalize_diagnosis_contract(raw: Any) -> Dict[str, Any]:
    safe = raw if isinstance(raw, dict) else {}

    def _normalize_list(value: Any, max_items: int = 6) -> List[str]:
        values: List[str] = []
        for item in _as_list(value):
            text = _as_str(item).strip()
            if not text:
                continue
            if text in values:
                continue
            values.append(text)
            if len(values) >= max_items:
                break
        return values

    return {
        "fault_summary": _as_str(safe.get("fault_summary")).strip(),
        "evidence_gaps": _normalize_list(safe.get("evidence_gaps")),
        "execution_plan": _normalize_list(safe.get("execution_plan"), max_items=8),
        "why_command_needed": _as_str(safe.get("why_command_needed")).strip(),
    }


def _diagnosis_contract_missing_fields(contract: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    safe_contract = _normalize_diagnosis_contract(contract)
    if not _as_str(safe_contract.get("fault_summary")).strip():
        missing.append("fault_summary")
    if len(_as_list(safe_contract.get("evidence_gaps"))) <= 0:
        missing.append("evidence_gaps")
    if len(_as_list(safe_contract.get("execution_plan"))) <= 0:
        missing.append("execution_plan")
    if not _as_str(safe_contract.get("why_command_needed")).strip():
        missing.append("why_command_needed")
    return missing


def _build_diagnosis_contract(
    *,
    answer: str,
    analysis_context: Dict[str, Any],
    reflection: Dict[str, Any],
    actions: List[Dict[str, Any]],
    fallback_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fallback = _normalize_diagnosis_contract(fallback_contract or {})
    safe_answer = _as_str(answer).strip()
    answer_lines = [line.strip() for line in safe_answer.splitlines() if line.strip()]
    overview_summary = _as_str(_extract_overview_summary(analysis_context)).strip()
    fault_summary = (
        _as_str(fallback.get("fault_summary")).strip()
        or overview_summary
        or (answer_lines[0] if answer_lines else "")
    )

    evidence_gaps = _as_list(fallback.get("evidence_gaps"))
    if not evidence_gaps:
        evidence_gaps = [
            _as_str(item).strip()
            for item in _as_list(reflection.get("gaps"))
            if _as_str(item).strip()
        ][:6]
    if not evidence_gaps:
        evidence_gaps = ["缺少可直接定位故障链路的关键日志/trace 证据"]

    execution_plan = _as_list(fallback.get("execution_plan"))
    if not execution_plan:
        derived_steps: List[str] = []
        for action in _as_list(actions):
            if not isinstance(action, dict):
                continue
            title = _as_str(action.get("title")).strip()
            command = _as_str(action.get("command")).strip()
            if title and command:
                derived_steps.append(f"{title}: {command}")
            elif title:
                derived_steps.append(title)
            elif command:
                derived_steps.append(command)
            if len(derived_steps) >= 8:
                break
        execution_plan = derived_steps
    if not execution_plan:
        execution_plan = ["补齐证据缺口后再执行针对性只读排查命令"]

    why_command_needed = (
        _as_str(fallback.get("why_command_needed")).strip()
        or (
            "当前结论仍存在证据缺口，需要通过只读命令补齐关键观测后再确认根因与修复路径。"
            if evidence_gaps
            else "需要执行只读命令验证当前故障假设并排除误判。"
        )
    )
    return _normalize_diagnosis_contract(
        {
            "fault_summary": fault_summary,
            "evidence_gaps": evidence_gaps,
            "execution_plan": execution_plan,
            "why_command_needed": why_command_needed,
        }
    )


async def _fill_diagnosis_contract_with_model(
    *,
    llm_enabled: bool,
    llm_requested: bool,
    timeout_seconds: int,
    question: str,
    answer: str,
    analysis_context: Dict[str, Any],
    reflection: Dict[str, Any],
    actions: List[Dict[str, Any]],
    current_contract: Dict[str, Any],
) -> Dict[str, Any]:
    if not llm_enabled or not llm_requested:
        return _normalize_diagnosis_contract(current_contract)
    llm_service = get_llm_service()
    prompt = (
        "请仅输出 JSON，补齐以下诊断合同字段，不要 markdown，不要解释。\n"
        "schema:\n"
        "{\n"
        '  "fault_summary":"string",\n'
        '  "evidence_gaps":["string"],\n'
        '  "execution_plan":["string"],\n'
        '  "why_command_needed":"string"\n'
        "}\n"
        "约束：四个字段必须非空；execution_plan 必须是可执行排查步骤；"
        "why_command_needed 必须明确“为何还要执行命令”。\n\n"
        f"问题:\n{_as_str(question)}\n\n"
        f"当前回答:\n{_as_str(answer)[:1800]}\n\n"
        f"当前诊断合同:\n{json.dumps(_normalize_diagnosis_contract(current_contract), ensure_ascii=False)}\n\n"
        f"反思缺口:\n{json.dumps(_as_list(reflection.get('gaps'))[:8], ensure_ascii=False)}\n\n"
        f"动作计划:\n{json.dumps(_as_list(actions)[:8], ensure_ascii=False)}\n\n"
        f"上下文摘要:\n{_as_str(_extract_overview_summary(analysis_context))[:600]}"
    )
    try:
        response_text = await asyncio.wait_for(
            llm_service.chat(
                message=prompt,
                context={
                    "task": "fill_diagnosis_contract",
                    "question": _as_str(question),
                    "analysis_context": analysis_context,
                    "reflection": reflection,
                    "actions": actions[:8],
                },
            ),
            timeout=max(6, int(timeout_seconds or 12)),
        )
        parsed = _parse_llm_json_dict(response_text, as_str=_as_str)
        if not isinstance(parsed, dict):
            return _normalize_diagnosis_contract(current_contract)
        merged = {
            **_normalize_diagnosis_contract(current_contract),
            **_normalize_diagnosis_contract(parsed),
        }
        return _normalize_diagnosis_contract(merged)
    except Exception:
        return _normalize_diagnosis_contract(current_contract)


def _as_runtime_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


_EVIDENCE_INSUFFICIENT_HINTS = [
    "证据不足",
    "无法定位",
    "无法确认",
    "仍缺失证据",
    "待补证据",
    "insufficient evidence",
    "cannot determine",
    "cannot confirm",
]


def _answer_declares_evidence_insufficient(text: Any) -> bool:
    safe_text = _as_str(text).strip().lower()
    if not safe_text:
        return False
    return any(token in safe_text for token in _EVIDENCE_INSUFFICIENT_HINTS)


def _soften_low_evidence_diagnosis_text(
    *,
    answer: Any,
    fault_summary: Any,
    react_loop: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Downgrade overconfident diagnosis wording when execution evidence is still weak."""
    loop = react_loop if isinstance(react_loop, dict) else {}
    observe = loop.get("observe") if isinstance(loop.get("observe"), dict) else {}
    execute = loop.get("execute") if isinstance(loop.get("execute"), dict) else {}
    plan_quality = loop.get("plan_quality") if isinstance(loop.get("plan_quality"), dict) else {}
    observed_actions = int(_as_float(execute.get("observed_actions"), 0))
    evidence_coverage = _as_float(observe.get("evidence_coverage"), _as_float(observe.get("coverage"), -1.0))
    final_confidence = _as_float(observe.get("final_confidence"), _as_float(observe.get("confidence"), -1.0))
    planning_blocked = bool(plan_quality.get("planning_blocked"))
    low_evidence = (
        planning_blocked
        or observed_actions <= 0
        or (evidence_coverage >= 0 and evidence_coverage < 0.2)
        or (final_confidence >= 0 and final_confidence < 0.4)
    )
    safe_answer = _as_str(answer).strip()
    safe_fault_summary = _as_str(fault_summary).strip()
    if not low_evidence:
        return safe_answer, safe_fault_summary

    disclaimer = "当前仅为待验证判断，尚未采集到足够执行证据。"

    def _soften_text(text: str, *, compact: bool = False) -> str:
        safe_text = _as_str(text).strip()
        if not safe_text:
            return disclaimer if compact else f"{disclaimer}\n\n"
        softened = safe_text
        softened = re.sub(r"(^|\n)结论：", r"\1初步判断（待验证）：", softened)
        softened = re.sub(r"(^|\n)根因分析：", r"\1待验证假设：", softened)
        softened = softened.replace("根因是", "当前更倾向于")
        softened = softened.replace("已确认", "初步怀疑")
        softened = softened.replace("可以确认", "当前更倾向于")
        softened = softened.replace("而非偶发事件", "但是否为持续性问题仍需继续验证")
        if _answer_declares_evidence_insufficient(softened):
            return softened
        if compact:
            return f"{disclaimer} {softened}"
        return f"{disclaimer}\n\n{softened}"

    return _soften_text(safe_answer), _soften_text(safe_fault_summary, compact=True)


def _resolve_ai_run_runtime_mode(
    analysis_context: Optional[Dict[str, Any]],
    runtime_options: Optional[Dict[str, Any]],
) -> str:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    safe_runtime_options = runtime_options if isinstance(runtime_options, dict) else {}
    explicit_mode = str(
        safe_runtime_options.get("mode")
        or safe_runtime_options.get("orchestration_mode")
        or safe_context.get("runtime_mode")
        or ""
    ).strip().lower()
    if explicit_mode:
        return explicit_mode
    agent_mode = str(safe_context.get("agent_mode") or "").strip().lower()
    if agent_mode in {"followup_runtime", "followup_analysis_runtime", "request_flow_runtime"}:
        return "followup_analysis"
    return "manual"


def _build_followup_request_from_ai_run(
    run: Any,
    runtime_options: Optional[Dict[str, Any]],
) -> "FollowUpRequest":
    safe_runtime_options = runtime_options if isinstance(runtime_options, dict) else {}
    history_items = safe_runtime_options.get("history")
    normalized_history: List[Dict[str, Any]] = []
    if isinstance(history_items, list):
        for item in history_items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            normalized_history.append(
                {
                    "role": role,
                    "content": content,
                    "timestamp": str(item.get("timestamp") or "").strip() or None,
                    "message_id": str(item.get("message_id") or "").strip() or None,
                    "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                }
            )
    analysis_context = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
    input_payload = getattr(run, "input_json", None)
    input_question = ""
    if isinstance(input_payload, dict):
        input_question = _as_str(input_payload.get("question")).strip()
    question = _as_str(getattr(run, "question", "")).strip() or input_question
    analysis_context = dict(analysis_context)
    analysis_context.setdefault("question", question)
    knowledge_selection = select_project_knowledge(analysis_context)
    analysis_context.update(knowledge_selection)
    run.context_json = analysis_context
    return FollowUpRequest(
        question=question,
        analysis_session_id=str(getattr(run, "session_id", "") or "").strip(),
        conversation_id=str(
            safe_runtime_options.get("conversation_id")
            or getattr(run, "conversation_id", "")
            or analysis_context.get("conversation_id")
            or ""
        ).strip(),
        use_llm=_as_runtime_bool(safe_runtime_options.get("use_llm"), True),
        show_thought=_as_runtime_bool(safe_runtime_options.get("show_thought"), True),
        auto_exec_readonly=_as_runtime_bool(safe_runtime_options.get("auto_exec_readonly"), True),
        analysis_context=analysis_context,
        history=normalized_history,
        reset=_as_runtime_bool(safe_runtime_options.get("reset"), False),
    )


def _build_project_knowledge_summary_updates(analysis_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    updates = {
        "knowledge_pack_version": _as_str(safe_context.get("knowledge_pack_version")),
        "knowledge_primary_service": _as_str(safe_context.get("knowledge_primary_service")),
        "knowledge_primary_path": _as_str(safe_context.get("knowledge_primary_path")),
        "knowledge_related_services": _as_list(safe_context.get("knowledge_related_services"))[:2],
        "knowledge_selection_reason": _as_str(safe_context.get("knowledge_selection_reason")),
    }
    filtered: Dict[str, Any] = {}
    for key, value in updates.items():
        if isinstance(value, list):
            if value:
                filtered[key] = value
            continue
        if value in {"", None}:
            continue
        filtered[key] = value
    return filtered


def _emit_runtime_stage_event(
    runtime_service: Any,
    run_id: str,
    *,
    step_id: str,
    phase: str,
    title: str,
    status: str,
    detail: str = "",
    iteration: Optional[int] = None,
) -> None:
    payload: Dict[str, Any] = {
        "step_id": step_id,
        "phase": phase,
        "title": title,
        "status": status,
    }
    if iteration is not None:
        payload["iteration"] = iteration
    runtime_service.append_event(run_id, event_protocol.REASONING_STEP, payload)
    safe_detail = str(detail or "").strip()
    if safe_detail:
        runtime_service.append_event(
            run_id,
            event_protocol.REASONING_SUMMARY_DELTA,
            {
                "step_id": step_id,
                "phase": phase,
                "text": safe_detail,
            },
        )


class _RuntimePauseForPendingAction(RuntimeError):
    """Signal follow-up runtime task to pause on pending approval/user-input action."""

    is_runtime_pause_signal = True


def _run_has_unresolved_pending_action(run: Any) -> bool:
    if run is None:
        return False
    status = _as_str(getattr(run, "status", "")).strip().lower()
    if status in {"waiting_approval", "waiting_user_input"}:
        return True
    summary = getattr(run, "summary_json", None)
    if not isinstance(summary, dict):
        return False
    pending_action = summary.get("pending_action")
    if isinstance(pending_action, dict):
        pending_status = _as_str(pending_action.get("status"), "pending").strip().lower()
        if pending_status == "pending":
            return True
    if int(_as_float(summary.get("pending_approval_count"), 0)) > 0:
        return True
    if isinstance(summary.get("pending_user_input"), dict):
        return True
    return False


def _resolve_runtime_planning_iteration(run: Any, payload: Dict[str, Any]) -> int:
    raw_iteration = payload.get("iteration")
    if isinstance(raw_iteration, (int, float)):
        return max(0, int(raw_iteration))
    summary = getattr(run, "summary_json", None)
    safe_summary = summary if isinstance(summary, dict) else {}
    return max(0, int(_as_float(safe_summary.get("iteration"), 0)))


def _build_runtime_planning_fingerprint(payload: Dict[str, Any]) -> str:
    safe_payload = payload if isinstance(payload, dict) else {}
    subgoals = safe_payload.get("subgoals") if isinstance(safe_payload.get("subgoals"), list) else []
    reflection = safe_payload.get("reflection") if isinstance(safe_payload.get("reflection"), dict) else {}
    normalized_subgoals: List[str] = []
    for item in subgoals:
        safe_item = item if isinstance(item, dict) else {}
        title = _as_str(safe_item.get("title")).strip()
        reason = _as_str(safe_item.get("reason")).strip()
        if title or reason:
            normalized_subgoals.append(f"{title}|{reason}")
    gaps = [_as_str(item).strip() for item in _as_list(reflection.get("gaps")) if _as_str(item).strip()]
    fingerprint_payload = {
        "subgoals": normalized_subgoals[:12],
        "gaps": gaps[:12],
    }
    return json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True)


def _merge_pending_command_request(existing: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing) if isinstance(existing, dict) else {}
    safe_updates = updates if isinstance(updates, dict) else {}
    for key, value in safe_updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip() and key in merged:
            continue
        if isinstance(value, dict) and not value and key in merged:
            continue
        merged[key] = value
    return merged


async def _emit_followup_runtime_event(
    runtime_service: Any,
    run_id: str,
    event_name: str,
    payload: Dict[str, Any],
    state: Dict[str, Any],
) -> None:
    safe_name = str(event_name or "").strip().lower()
    safe_payload = payload if isinstance(payload, dict) else {}
    run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
    if run is None or is_terminal_run_status(run.status):
        return
    if _run_has_unresolved_pending_action(run):
        raise _RuntimePauseForPendingAction("pending_action_exists")

    if safe_name == "token":
        runtime_service.append_assistant_delta(
            run_id,
            text=str(safe_payload.get("text") or ""),
            assistant_message_id=run.assistant_message_id,
        )
        return

    if safe_name == "plan":
        stage = str(safe_payload.get("stage") or "").strip().lower() or "planning"
        current_iteration = _resolve_runtime_planning_iteration(run, safe_payload)
        if bool(state.get("suppress_bootstrap_plan")) and stage in {
            "session_prepare",
            "history_load",
            "long_term_memory",
            "react_memory_load",
        }:
            runtime_service._update_run_summary(  # noqa: SLF001
                run,
                current_phase="planning",
            )
            return
        title_map = {
            "session_prepare": "准备分析会话",
            "history_load": "加载上下文历史",
            "long_term_memory": "检索长期记忆",
            "react_memory_load": "加载执行记忆",
            "planning_ready": "完成问题拆解",
            "llm_start": "开始生成回答",
        }
        detail = ""
        if stage == "planning_ready":
            planning_fingerprint = _build_runtime_planning_fingerprint(safe_payload)
            summary_payload = run.summary_json if isinstance(getattr(run, "summary_json", None), dict) else {}
            last_planning_fingerprint = _as_str(summary_payload.get("last_planning_ready_fingerprint")).strip()
            last_planning_iteration = int(_as_float(summary_payload.get("last_planning_ready_iteration"), -1))
            state["suppress_planning_ready_thought_once"] = True
            if (
                planning_fingerprint
                and planning_fingerprint == last_planning_fingerprint
                and current_iteration == last_planning_iteration
            ):
                suppressed_count = max(0, int(_as_float(summary_payload.get("planning_ready_suppressed_count"), 0))) + 1
                runtime_service._update_run_summary(  # noqa: SLF001
                    run,
                    current_phase="planning",
                    planning_ready_suppressed_count=suppressed_count,
                    last_planning_ready_iteration=current_iteration,
                )
                return
            detail = f"子目标 {len(safe_payload.get('subgoals') or [])} 项"
            runtime_service._update_run_summary(  # noqa: SLF001
                run,
                last_planning_ready_fingerprint=planning_fingerprint,
                last_planning_ready_iteration=current_iteration,
            )
        elif stage == "llm_start":
            detail = "已进入回答生成阶段"
        _emit_runtime_stage_event(
            runtime_service,
            run_id,
            step_id=f"plan-{stage}",
            phase="planning",
            title=title_map.get(stage, stage or "规划阶段"),
            status="completed" if stage in {"planning_ready", "llm_start"} else "in_progress",
            detail=detail,
            iteration=current_iteration,
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            current_phase="planning",
        )
        return

    if safe_name == "thought":
        thought_title = str(safe_payload.get("title") or "").strip()
        thought_phase = str(safe_payload.get("phase") or "thought").strip().lower()
        if bool(state.get("suppress_action_plan_thought_once")):
            state["suppress_action_plan_thought_once"] = False
            if thought_title.startswith("生成执行计划") and thought_phase in {"action", "plan", "planning"}:
                return
        if bool(state.get("suppress_planning_ready_thought_once")):
            state["suppress_planning_ready_thought_once"] = False
            if thought_title.startswith("完成问题拆解") and thought_phase in {"plan", "planning"}:
                return
        state["thought_index"] = int(state.get("thought_index") or 0) + 1
        step_id = str(safe_payload.get("step_id") or "").strip() or f"thought-{state['thought_index']:04d}"
        phase = str(safe_payload.get("phase") or "thought").strip() or "thought"
        title = thought_title or "执行中"
        detail = str(safe_payload.get("detail") or "").strip()
        iteration = safe_payload.get("iteration")
        normalized_iteration = int(iteration) if isinstance(iteration, (int, float)) else None
        _emit_runtime_stage_event(
            runtime_service,
            run_id,
            step_id=step_id,
            phase=phase,
            title=title,
            status=str(safe_payload.get("status") or "info").strip() or "info",
            detail=detail,
            iteration=normalized_iteration,
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            current_phase=phase,
            iteration=normalized_iteration if normalized_iteration is not None else (run.summary_json or {}).get("iteration", 0),
        )
        return

    if safe_name == "action":
        actions = safe_payload.get("actions") if isinstance(safe_payload.get("actions"), list) else []
        state["actions"] = actions
        state["suppress_action_plan_thought_once"] = True
        _emit_runtime_stage_event(
            runtime_service,
            run_id,
            step_id="action-plan",
            phase="action",
            title=f"生成执行计划 {len(actions)} 项",
            status="completed",
            detail="已生成可执行动作建议。",
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            current_phase="action",
            actions=actions,
        )
        return

    if safe_name == "action_spec_validated":
        runtime_service.append_event(
            run_id,
            "action_spec_validated",
            {
                "tool_call_id": str(safe_payload.get("tool_call_id") or "").strip(),
                "action_id": str(safe_payload.get("action_id") or "").strip(),
                "title": str(safe_payload.get("title") or "结构化命令已校验").strip() or "结构化命令已校验",
                "command": str(safe_payload.get("command") or "").strip(),
                "message": str(safe_payload.get("message") or "command_spec validated").strip(),
                "command_spec": safe_payload.get("command_spec") if isinstance(safe_payload.get("command_spec"), dict) else {},
            },
        )
        return

    if safe_name == "action_preflight_failed":
        runtime_service.append_event(
            run_id,
            "action_preflight_failed",
            {
                "tool_call_id": str(safe_payload.get("tool_call_id") or "").strip(),
                "action_id": str(safe_payload.get("action_id") or "").strip(),
                "title": str(safe_payload.get("title") or "预检失败").strip() or "预检失败",
                "command": str(safe_payload.get("command") or "").strip(),
                "error_code": str(safe_payload.get("error_code") or "").strip(),
                "message": str(safe_payload.get("message") or "").strip(),
            },
        )
        return

    if safe_name == "action_execution_retrying":
        _metric_inc(AI_RUNTIME_TIMEOUT_RETRY_TOTAL)
        runtime_service.append_event(
            run_id,
            "action_execution_retrying",
            {
                "tool_call_id": str(safe_payload.get("tool_call_id") or "").strip(),
                "action_id": str(safe_payload.get("action_id") or "").strip(),
                "title": str(safe_payload.get("title") or "命令重试中").strip() or "命令重试中",
                "command": str(safe_payload.get("command") or "").strip(),
                "attempt": int(_as_float(safe_payload.get("attempt"), 0)),
                "max_attempts": int(_as_float(safe_payload.get("max_attempts"), 0)),
                "message": str(safe_payload.get("message") or "").strip(),
            },
        )
        return

    if safe_name == "observation":
        action_id = str(safe_payload.get("action_id") or "").strip()
        command = str(safe_payload.get("command") or "").strip()
        purpose = str(safe_payload.get("purpose") or "").strip()
        command_run_id = str(safe_payload.get("command_run_id") or "").strip()
        reason_code = str(safe_payload.get("reason_code") or "").strip().lower()
        normalized_command_for_key = _normalize_followup_command_line(command) or command
        normalized_status = str(safe_payload.get("status") or "").strip().lower() or "running"
        if command_run_id:
            observation_key = f"command_run:{command_run_id}"
        elif action_id or normalized_command_for_key:
            observation_key = "|".join(
                [
                    f"action:{action_id or '<none>'}",
                    f"command:{normalized_command_for_key or '<none>'}",
                    f"status:{normalized_status or '<none>'}",
                    f"reason:{reason_code or '<none>'}",
                ]
            )
        else:
            observation_key = f"observation-{len(state.get('tool_call_ids', {})) + 1}"
        tool_call_ids = state.setdefault("tool_call_ids", {})
        tool_call_id = str(tool_call_ids.get(observation_key) or "").strip()
        if not tool_call_id:
            tool_call_id = f"tool-auto-{len(tool_call_ids) + 1:04d}"
            tool_call_ids[observation_key] = tool_call_id
        effective_action_id = action_id or command_run_id or observation_key

        status_map = {
            "executed": "completed",
            "completed": "completed",
            "running": "running",
            "failed": "failed",
            "timed_out": "timed_out",
            "skipped": "skipped",
            "cancelled": "cancelled",
        }
        event_status = status_map.get(normalized_status, normalized_status)
        exit_code_value = int(_as_float(safe_payload.get("exit_code"), 0))
        attempt_value = int(_as_float(safe_payload.get("attempt"), 0))
        max_attempts_value = int(_as_float(safe_payload.get("max_attempts"), 0))
        timed_out = bool(safe_payload.get("timed_out")) or exit_code_value in {-9, -15}
        if timed_out and event_status in {"completed", "failed", ""}:
            event_status = "timed_out"
        next_suggestion = str(safe_payload.get("next_suggestion") or "").strip()
        if timed_out and exit_code_value == -9 and not next_suggestion:
            next_suggestion = "建议先缩小时间窗口或 limit，再提高 timeout 重试。"
        if reason_code in {"duplicate_skipped", "duplicate_skipped_attempt"}:
            reused_evidence_ids = _as_list(safe_payload.get("reused_evidence_ids"))
            has_reuse_source = bool(command_run_id) or any(_as_str(item).strip() for item in reused_evidence_ids)
            finished_key = "|".join(
                [
                    tool_call_id,
                    event_status or "skipped",
                    command_run_id,
                    reason_code,
                ]
            )
            finished_tool_calls = state.setdefault("finished_tool_calls", set())
            if finished_key in finished_tool_calls:
                return
            finished_tool_calls.add(finished_key)
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": "command.exec",
                    "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                    "status": event_status or "skipped_duplicate",
                    "action_id": effective_action_id,
                    "command_run_id": command_run_id,
                    "reused_command_run_id": command_run_id,
                    "command": command,
                    "purpose": purpose,
                    "command_type": str(safe_payload.get("command_type") or "").strip(),
                    "risk_level": str(safe_payload.get("risk_level") or "").strip(),
                    "message": str(safe_payload.get("message") or "").strip(),
                    "reason_code": reason_code,
                    "evidence_reuse": (
                        bool(safe_payload.get("evidence_reuse"))
                        or (reason_code == "duplicate_skipped" and has_reuse_source)
                    ),
                    "reused_evidence_ids": reused_evidence_ids,
                    "evidence_slot_id": str(safe_payload.get("evidence_slot_id") or "").strip(),
                    "evidence_outcome": str(
                        safe_payload.get("evidence_outcome")
                        or ("reused" if has_reuse_source else "missing")
                    ).strip(),
                    "info_gain_score": _as_float(safe_payload.get("info_gain_score"), 0.0),
                },
            )
            return
        if tool_call_id not in state.setdefault("started_tool_calls", set()):
            state["started_tool_calls"].add(tool_call_id)
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_STARTED,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": "command.exec",
                    "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                    "status": "running" if event_status == "running" else event_status,
                    "action_id": effective_action_id,
                    "command_run_id": command_run_id,
                    "command": command,
                    "command_type": str(safe_payload.get("command_type") or "").strip(),
                    "risk_level": str(safe_payload.get("risk_level") or "").strip(),
                },
            )
        if str(safe_payload.get("text") or "").strip():
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_OUTPUT_DELTA,
                {
                    "tool_call_id": tool_call_id,
                    "action_id": effective_action_id,
                    "command_run_id": command_run_id,
                    "command": command,
                    "stream": str(safe_payload.get("stream") or "stdout").strip() or "stdout",
                    "text": str(safe_payload.get("text") or ""),
                    "output_truncated": bool(safe_payload.get("output_truncated")),
                },
            )
        if event_status == "running":
            runtime_service.append_event(
                run_id,
                event_protocol.TOOL_CALL_PROGRESS,
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": "command.exec",
                    "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                    "status": "running",
                    "action_id": effective_action_id,
                    "command_run_id": command_run_id,
                    "command": command,
                    "message": str(safe_payload.get("message") or "").strip(),
                },
            )
            return
        finished_key = "|".join(
            [
                tool_call_id,
                event_status or "completed",
                command_run_id,
                reason_code,
            ]
        )
        finished_tool_calls = state.setdefault("finished_tool_calls", set())
        if finished_key in finished_tool_calls:
            return
        finished_tool_calls.add(finished_key)
        runtime_service.append_event(
            run_id,
            event_protocol.TOOL_CALL_FINISHED,
            {
                "tool_call_id": tool_call_id,
                "tool_name": "command.exec",
                "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                "status": event_status or "completed",
                "action_id": effective_action_id,
                "command_run_id": command_run_id,
                "command": command,
                "command_type": str(safe_payload.get("command_type") or "").strip(),
                "risk_level": str(safe_payload.get("risk_level") or "").strip(),
                "message": str(safe_payload.get("message") or "").strip(),
                "stdout": str(safe_payload.get("stdout") or ""),
                "stderr": str(safe_payload.get("stderr") or ""),
                "exit_code": exit_code_value,
                "timed_out": timed_out,
                "output_truncated": bool(safe_payload.get("output_truncated")),
                "next_suggestion": next_suggestion,
                "attempt": attempt_value,
                "max_attempts": max_attempts_value,
                "reason_code": reason_code,
                "evidence_reuse": bool(safe_payload.get("evidence_reuse")),
                "reused_evidence_ids": _as_list(safe_payload.get("reused_evidence_ids")),
                "evidence_slot_id": str(safe_payload.get("evidence_slot_id") or "").strip(),
                "evidence_outcome": str(safe_payload.get("evidence_outcome") or "").strip(),
                "evidence_slot_ids_filled": _as_list(safe_payload.get("evidence_slot_ids_filled")),
                "info_gain_score": _as_float(safe_payload.get("info_gain_score"), 0.0),
            },
        )
        if event_status == "completed" and not timed_out and attempt_value > 1:
            _metric_inc(AI_RUNTIME_TIMEOUT_RETRY_SUCCESS_TOTAL)
        if event_status == "completed" and exit_code_value == 0 and command:
            latest_run = runtime_service.get_run(run_id)
            if latest_run is not None:
                summary_payload = latest_run.summary_json if isinstance(latest_run.summary_json, dict) else {}
                executed_commands = [
                    _normalize_followup_command_line(item)
                    for item in _as_list(summary_payload.get("executed_commands"))
                    if _normalize_followup_command_line(item)
                ]
                normalized_command = _normalize_followup_command_line(command)
                if normalized_command and normalized_command not in executed_commands:
                    executed_commands.append(normalized_command)
                    runtime_service._update_run_summary(  # noqa: SLF001
                        latest_run,
                        executed_commands=executed_commands[-200:],
                    )
        command_type = str(safe_payload.get("command_type") or "").strip().lower()
        gate_status = normalized_status
        gate_message = str(safe_payload.get("message") or "").strip()
        if gate_status in {"confirmation_required", "elevation_required"}:
            latest_run = runtime_service.get_run(run_id)
            if (
                latest_run is not None
                and not is_terminal_run_status(latest_run.status)
                and str(latest_run.status).strip().lower() not in {"waiting_approval", "waiting_user_input"}
                and command
            ):
                latest_summary = (
                    latest_run.summary_json if isinstance(getattr(latest_run, "summary_json", None), dict) else {}
                )
                last_user_input = (
                    latest_summary.get("last_user_input")
                    if isinstance(latest_summary.get("last_user_input"), dict)
                    else {}
                )
                existing_pending = (
                    latest_summary.get("pending_command_request")
                    if isinstance(latest_summary.get("pending_command_request"), dict)
                    else {}
                )
                payload_command_spec = (
                    safe_payload.get("command_spec")
                    if isinstance(safe_payload.get("command_spec"), dict)
                    else {}
                )
                existing_command_spec = (
                    existing_pending.get("command_spec")
                    if isinstance(existing_pending.get("command_spec"), dict)
                    else {}
                )
                effective_command_spec = payload_command_spec if payload_command_spec else existing_command_spec
                payload_diagnosis_contract = (
                    safe_payload.get("diagnosis_contract")
                    if isinstance(safe_payload.get("diagnosis_contract"), dict)
                    else {}
                )
                if command_type in {"", "unknown"}:
                    question_payload = build_business_question(
                        failure_code="unknown_semantics",
                        failure_message=gate_message or "当前动作未提供可执行命令",
                        purpose=purpose or command,
                        title=str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                        command=command,
                        current_action_id=action_id or tool_call_id,
                        last_user_input_question_kind=str(last_user_input.get("question_kind") or "").strip(),
                        last_user_input_action_id=str(last_user_input.get("action_id") or "").strip(),
                        last_user_input_text=(
                            str(last_user_input.get("business_answer_text") or "").strip()
                            or str(last_user_input.get("text") or "").strip()
                        ),
                    )
                    runtime_service._update_run_summary(  # noqa: SLF001
                        latest_run,
                        pending_command_request=_merge_pending_command_request(
                            existing_pending,
                            {
                                "tool_call_id": tool_call_id,
                                "action_id": action_id,
                                "command": command,
                                "purpose": purpose or command,
                                "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                                "tool_name": "command.exec",
                                "status": gate_status,
                                "message": gate_message or "当前动作未提供可执行命令",
                                "command_spec": effective_command_spec,
                                "diagnosis_contract": payload_diagnosis_contract,
                            },
                        ),
                    )
                    runtime_service.request_user_input(
                        run_id,
                        action_id=action_id or tool_call_id,
                        title=str(question_payload.get("title") or "").strip(),
                        prompt=str(question_payload.get("prompt") or "").strip(),
                        reason=str(question_payload.get("reason") or "").strip(),
                        command=command,
                        purpose=purpose or command,
                        kind="business_question",
                        question_kind=str(question_payload.get("question_kind") or "").strip(),
                    )
                else:
                    pending_command_request = _merge_pending_command_request(
                        existing_pending,
                        {
                            "tool_call_id": tool_call_id,
                            "action_id": action_id,
                            "command": command,
                            "purpose": purpose or command,
                            "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                            "tool_name": "command.exec",
                            "timeout_seconds": int(safe_payload.get("timeout_seconds") or 20),
                            "confirmation_ticket": str(
                                safe_payload.get("confirmation_ticket")
                                or safe_payload.get("approval_id")
                                or ""
                            ).strip(),
                            "command_spec": effective_command_spec,
                            "diagnosis_contract": payload_diagnosis_contract,
                        },
                    )
                    runtime_service._update_run_summary(  # noqa: SLF001
                        latest_run,
                        pending_command_request=pending_command_request,
                    )
                    runtime_service.request_approval(
                        run_id,
                        approval_id=str(
                            safe_payload.get("confirmation_ticket")
                            or safe_payload.get("approval_id")
                            or ""
                        ).strip() or build_id("apr"),
                        title=str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                        reason=gate_message or "命令需要人工确认后继续执行",
                        command=command,
                        purpose=purpose or command,
                        command_type=command_type or "unknown",
                        risk_level=str(safe_payload.get("risk_level") or "high").strip() or "high",
                        command_family=str(safe_payload.get("command_family") or "unknown").strip() or "unknown",
                        approval_policy=str(safe_payload.get("approval_policy") or gate_status).strip() or gate_status,
                        executor_type=str(safe_payload.get("executor_type") or "local_process").strip() or "local_process",
                        executor_profile=str(safe_payload.get("executor_profile") or "local-default").strip() or "local-default",
                        target_kind=str(safe_payload.get("target_kind") or "runtime_node").strip() or "runtime_node",
                        target_identity=str(safe_payload.get("target_identity") or "runtime:local").strip() or "runtime:local",
                        requires_confirmation=bool(safe_payload.get("requires_confirmation")) or gate_status in {"confirmation_required", "elevation_required"},
                        requires_elevation=bool(safe_payload.get("requires_elevation")) or gate_status == "elevation_required",
                    )
                raise _RuntimePauseForPendingAction("pending_action_created")
        if gate_status in {"semantic_incomplete", "permission_required"} and command_type in {"", "unknown"}:
            latest_run = runtime_service.get_run(run_id)
            if (
                latest_run is not None
                and not is_terminal_run_status(latest_run.status)
                and str(latest_run.status).strip().lower() not in {"waiting_approval", "waiting_user_input"}
            ):
                latest_summary = (
                    latest_run.summary_json if isinstance(getattr(latest_run, "summary_json", None), dict) else {}
                )
                last_user_input = (
                    latest_summary.get("last_user_input")
                    if isinstance(latest_summary.get("last_user_input"), dict)
                    else {}
                )
                existing_pending = (
                    latest_summary.get("pending_command_request")
                    if isinstance(latest_summary.get("pending_command_request"), dict)
                    else {}
                )
                payload_command_spec = (
                    safe_payload.get("command_spec")
                    if isinstance(safe_payload.get("command_spec"), dict)
                    else {}
                )
                existing_command_spec = (
                    existing_pending.get("command_spec")
                    if isinstance(existing_pending.get("command_spec"), dict)
                    else {}
                )
                effective_command_spec = payload_command_spec if payload_command_spec else existing_command_spec
                payload_diagnosis_contract = (
                    safe_payload.get("diagnosis_contract")
                    if isinstance(safe_payload.get("diagnosis_contract"), dict)
                    else {}
                )
                gate_reason = (gate_message or "").strip().lower()
                failure_code = (
                    "sql_preflight_failed"
                    if "sql_preflight_failed" in gate_reason
                    else ("semantic_incomplete" if gate_status == "semantic_incomplete" else "unknown_semantics")
                )
                question_payload = build_business_question(
                    failure_code=failure_code,
                    failure_message=gate_message or "当前动作未提供可执行命令",
                    purpose=purpose or command,
                    title=str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                    command=command,
                    current_action_id=action_id or tool_call_id,
                    last_user_input_question_kind=str(last_user_input.get("question_kind") or "").strip(),
                    last_user_input_action_id=str(last_user_input.get("action_id") or "").strip(),
                    last_user_input_text=(
                        str(last_user_input.get("business_answer_text") or "").strip()
                        or str(last_user_input.get("text") or "").strip()
                    ),
                )
                runtime_service._update_run_summary(  # noqa: SLF001
                    latest_run,
                    pending_command_request=_merge_pending_command_request(
                        existing_pending,
                        {
                            "tool_call_id": tool_call_id,
                            "action_id": action_id,
                            "command": command,
                            "purpose": purpose or command,
                            "title": str(safe_payload.get("title") or command or "执行命令").strip() or "执行命令",
                            "tool_name": "command.exec",
                            "status": gate_status,
                            "message": gate_message or "当前动作未提供可执行命令",
                            "command_spec": effective_command_spec,
                            "diagnosis_contract": payload_diagnosis_contract,
                        },
                    ),
                )
                runtime_service.request_user_input(
                    run_id,
                    action_id=action_id or tool_call_id,
                    title=str(question_payload.get("title") or "").strip(),
                    prompt=str(question_payload.get("prompt") or "").strip(),
                    reason=str(question_payload.get("reason") or "").strip(),
                    command=command,
                    purpose=purpose or command,
                    kind="business_question",
                    question_kind=str(question_payload.get("question_kind") or "").strip(),
                )
                raise _RuntimePauseForPendingAction("pending_user_input_created")
        return

    if safe_name == "approval_required":
        approvals = state.setdefault("approval_required", [])
        approvals.append(safe_payload)
        _emit_runtime_stage_event(
            runtime_service,
            run_id,
            step_id=f"approval-{len(approvals):04d}",
            phase="approval",
            title="发现需要人工确认的动作",
            status="warning",
            detail=str(safe_payload.get("command") or safe_payload.get("message") or "").strip(),
        )
        runtime_service._update_run_summary(  # noqa: SLF001
            run,
            current_phase="approval",
            approval_required=approvals,
        )
        return

    if safe_name == "replan":
        react_loop = safe_payload.get("react_loop") if isinstance(safe_payload.get("react_loop"), dict) else {}
        state["react_loop"] = react_loop
        _emit_runtime_stage_event(
            runtime_service,
            run_id,
            step_id="replan-evaluation",
            phase="replan",
            title="执行结果闭环评估",
            status="warning" if bool((react_loop.get("replan") or {}).get("needed")) else "success",
            detail=str(react_loop.get("summary") or "").strip(),
        )


async def _run_followup_runtime_task(
    runtime_service: Any,
    run_id: str,
    runtime_options: Optional[Dict[str, Any]],
) -> None:
    run = runtime_service.get_run(run_id)
    if run is None:
        return
    summary_payload = run.summary_json if isinstance(getattr(run, "summary_json", None), dict) else {}
    followup_task_count = max(0, int(_as_float(summary_payload.get("followup_runtime_task_count"), 0))) + 1
    suppress_bootstrap_plan = followup_task_count > 1
    previous_executed_commands = [
        _normalize_followup_command_line(item)
        for item in _as_list(summary_payload.get("executed_commands"))
        if _normalize_followup_command_line(item)
    ]
    previous_action_observations = [
        item
        for item in _as_list(summary_payload.get("action_observations"))
        if isinstance(item, dict)
    ]
    runtime_service._update_run_summary(  # noqa: SLF001
        run,
        followup_runtime_worker="running",
        followup_runtime_task_count=followup_task_count,
        followup_runtime_last_started_at=_utc_now_iso(),
    )
    runtime_state: Dict[str, Any] = {
        "thought_index": 0,
        "tool_call_ids": {},
        "started_tool_calls": set(),
        "finished_tool_calls": set(),
        "actions": [],
        "approval_required": [],
        "react_loop": {},
        "suppress_bootstrap_plan": suppress_bootstrap_plan,
    }

    async def _runtime_event_callback(event_name: str, payload: Dict[str, Any]) -> None:
        await _emit_followup_runtime_event(
            runtime_service,
            run_id,
            event_name,
            payload,
            runtime_state,
        )

    try:
        followup_request = _build_followup_request_from_ai_run(run, runtime_options)
        knowledge_updates = _build_project_knowledge_summary_updates(followup_request.analysis_context)
        if knowledge_updates:
            runtime_service._update_run_summary(run, **knowledge_updates)  # noqa: SLF001
        if previous_executed_commands:
            followup_request.analysis_context = {
                **(followup_request.analysis_context or {}),
                "_runtime_executed_commands": previous_executed_commands,
            }
        if previous_action_observations:
            followup_request.analysis_context = {
                **(followup_request.analysis_context or {}),
                "_runtime_prior_action_observations": previous_action_observations[-200:],
            }
        result = await _run_follow_up_analysis_core(
            followup_request,
            event_callback=_runtime_event_callback,
        )
        latest_run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
        if latest_run is None or is_terminal_run_status(latest_run.status):
            return
        if _run_has_unresolved_pending_action(latest_run):
            return
        assistant_metadata = {
            "analysis_session_id": str(result.get("analysis_session_id") or ""),
            "conversation_id": str(result.get("conversation_id") or ""),
            "analysis_method": str(result.get("analysis_method") or ""),
            "followup_engine": str(result.get("followup_engine") or ""),
            "actions": result.get("actions") if isinstance(result.get("actions"), list) else runtime_state.get("actions", []),
            "action_observations": result.get("action_observations") if isinstance(result.get("action_observations"), list) else [],
            "react_loop": result.get("react_loop") if isinstance(result.get("react_loop"), dict) else runtime_state.get("react_loop", {}),
            "react_iterations": result.get("react_iterations") if isinstance(result.get("react_iterations"), list) else [],
            "subgoals": result.get("subgoals") if isinstance(result.get("subgoals"), list) else [],
            "reflection": result.get("reflection") if isinstance(result.get("reflection"), dict) else {},
            "thoughts": result.get("thoughts") if isinstance(result.get("thoughts"), list) else [],
            "context_pills": result.get("context_pills") if isinstance(result.get("context_pills"), list) else [],
            "approval_required": runtime_state.get("approval_required", []),
        }
        softened_answer, softened_fault_summary = _soften_low_evidence_diagnosis_text(
            answer=result.get("answer"),
            fault_summary=result.get("fault_summary"),
            react_loop=assistant_metadata.get("react_loop"),
        )
        result["answer"] = softened_answer
        if softened_fault_summary:
            result["fault_summary"] = softened_fault_summary
        runtime_service.finalize_assistant_message(
            run_id,
            assistant_message_id=latest_run.assistant_message_id,
            content=str(result.get("answer") or ""),
            references=result.get("references") if isinstance(result.get("references"), list) else [],
            metadata=assistant_metadata,
        )
        latest_run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
        if latest_run is None or is_terminal_run_status(latest_run.status):
            return
        if _run_has_unresolved_pending_action(latest_run):
            return
        react_loop_payload = (
            assistant_metadata["react_loop"]
            if isinstance(assistant_metadata.get("react_loop"), dict)
            else {}
        )
        react_replan_payload = (
            react_loop_payload.get("replan")
            if isinstance(react_loop_payload.get("replan"), dict)
            else {}
        )
        plan_payload = (
            react_loop_payload.get("plan")
            if isinstance(react_loop_payload.get("plan"), dict)
            else {}
        )
        execute_payload = (
            react_loop_payload.get("execute")
            if isinstance(react_loop_payload.get("execute"), dict)
            else {}
        )
        react_replan_needed = bool(react_replan_payload.get("needed"))
        subgoals_payload = assistant_metadata["subgoals"] if isinstance(assistant_metadata.get("subgoals"), list) else []
        has_needs_data_subgoal = any(
            _as_str((item or {}).get("status")).strip().lower() == "needs_data"
            for item in subgoals_payload
            if isinstance(item, dict)
        )
        observe_payload = (
            react_loop_payload.get("observe")
            if isinstance(react_loop_payload.get("observe"), dict)
            else {}
        )
        plan_quality_payload = (
            react_loop_payload.get("plan_quality")
            if isinstance(react_loop_payload.get("plan_quality"), dict)
            else {}
        )
        runtime_options_payload = runtime_options if isinstance(runtime_options, dict) else {}
        allow_auto_exec_readonly = _as_runtime_bool(runtime_options_payload.get("auto_exec_readonly"), True)
        react_replan_items = [
            item
            for item in _as_list(react_replan_payload.get("items"))
            if isinstance(item, dict)
        ]
        action_observations_payload = [
            item
            for item in _as_list(assistant_metadata.get("action_observations"))
            if isinstance(item, dict)
        ]
        derived_ready_template_actions = sum(
            1
            for item in _as_list(assistant_metadata.get("actions"))
            if isinstance(item, dict)
            and bool(item.get("executable"))
            and _as_str(item.get("source")).strip().lower() == "template_command"
        )
        derived_observed_actions = len(action_observations_payload)
        ready_template_actions = max(
            int(_as_float(plan_payload.get("ready_template_actions"), 0)),
            derived_ready_template_actions,
        )
        observed_actions = max(
            int(_as_float(execute_payload.get("observed_actions"), 0)),
            derived_observed_actions,
        )
        planning_blocked = bool(plan_quality_payload.get("planning_blocked"))
        backend_unready = any(
            _as_str(item.get("execution_disposition")).strip().lower() == "backend_unready"
            for item in react_replan_items
        ) or any(
            _as_str(item.get("reason_code")).strip().lower() == "backend_unready"
            for item in action_observations_payload
        )
        evidence_slot_map = (
            observe_payload.get("evidence_slot_map")
            if isinstance(observe_payload.get("evidence_slot_map"), dict)
            else {}
        )
        evidence_slot_missing_or_partial = [
            _as_str(slot_id).strip()
            for slot_id, slot_payload in evidence_slot_map.items()
            if _as_str(slot_id).strip()
            and _as_str((slot_payload or {}).get("status")).strip().lower() in {"missing", "partial"}
        ]
        legacy_coverage_value = _as_float(observe_payload.get("coverage"), -1.0)
        evidence_coverage_value = _as_float(observe_payload.get("evidence_coverage"), legacy_coverage_value)
        min_coverage = max(0.0, min(1.0, _as_float(os.getenv("AI_RUNTIME_MIN_EVIDENCE_COVERAGE"), 0.6)))
        coverage_insufficient = evidence_coverage_value >= 0 and evidence_coverage_value < min_coverage

        legacy_confidence_value = _as_float(observe_payload.get("confidence"), -1.0)
        final_confidence_value = _as_float(observe_payload.get("final_confidence"), legacy_confidence_value)
        min_final_confidence = max(0.0, min(1.0, _as_float(os.getenv("AI_RUNTIME_MIN_FINAL_CONFIDENCE"), 0.0)))
        confidence_insufficient = (
            min_final_confidence > 0
            and final_confidence_value >= 0
            and final_confidence_value < min_final_confidence
        )
        answer_text = _as_str(result.get("answer"))
        fault_summary_text = _as_str(result.get("fault_summary"))
        answer_declares_insufficient = (
            _answer_declares_evidence_insufficient(answer_text)
            or _answer_declares_evidence_insufficient(fault_summary_text)
        )
        missing_evidence_slots = [
            _as_str(item).strip()
            for item in _as_list(observe_payload.get("missing_evidence_slots"))
            if _as_str(item).strip()
        ]
        for slot_id in evidence_slot_missing_or_partial:
            if slot_id and slot_id not in missing_evidence_slots:
                missing_evidence_slots.append(slot_id)
        evidence_slots_missing = len(missing_evidence_slots) > 0
        coverage_insufficient = coverage_insufficient or evidence_slots_missing
        evidence_incomplete = (
            has_needs_data_subgoal
            or coverage_insufficient
            or confidence_insufficient
            or answer_declares_insufficient
            or evidence_slots_missing
        )
        def _compact_blocked_reason_detail(value: Any, fallback: str = "") -> str:
            text = _as_str(value).strip() or fallback
            return text[:240]

        blocked_reason = ""
        blocked_reason_detail = ""
        if ready_template_actions > 0 and observed_actions <= 0 and not allow_auto_exec_readonly:
            blocked_reason = "readonly_auto_exec_disabled"
            blocked_reason_detail = "当前运行已禁用只读自动执行，请手动执行或开启自动执行后继续。"
        elif ready_template_actions > 0 and backend_unready:
            blocked_reason = "backend_unready"
            blocked_reason_detail = "执行网关未就绪，模板命令暂未自动执行，请稍后重试。"
        elif ready_template_actions <= 0 and planning_blocked:
            blocked_reason = "planning_incomplete"
            blocked_reason_detail = _compact_blocked_reason_detail(plan_quality_payload.get("planning_blocked_reason"), (
                "当前命令计划大多不可执行，应先修复结构化命令再继续闭环。"
            ))
        elif ready_template_actions > 0 and observed_actions <= 0:
            blocked_reason = "observation_missing"
            blocked_reason_detail = "已生成可执行模板命令，但尚未获得执行观察结果。"
        elif react_replan_needed:
            blocked_reason = "react_replan_needed"
            next_actions = [
                _as_str(item).strip()
                for item in _as_list(react_replan_payload.get("next_actions"))
                if _as_str(item).strip()
            ]
            blocked_reason_detail = _compact_blocked_reason_detail(
                next_actions[0] if next_actions else "",
                "关键证据仍未补齐，当前需继续执行建议动作。",
            )
        elif evidence_incomplete:
            blocked_reason = "evidence_incomplete"
            blocked_reason_detail = "当前证据覆盖度或结论置信度不足，需继续补齐证据。"
        final_status = "blocked" if (bool(blocked_reason) or evidence_incomplete) else "completed"
        next_best_commands = [
            item
            for item in _as_list((react_replan_payload or {}).get("next_best_commands"))
            if isinstance(item, dict)
        ]
        if not next_best_commands:
            next_best_commands = [
                item
                for item in _as_list(observe_payload.get("next_best_commands"))
                if isinstance(item, dict)
            ]
        diagnosis_status = "blocked" if final_status == "blocked" else "completed"
        if final_status != "blocked":
            if evidence_coverage_value >= 0 and evidence_coverage_value < 1:
                diagnosis_status = "partial"
            if final_confidence_value >= 0.75:
                diagnosis_status = "confirmed"
            elif final_confidence_value >= 0:
                diagnosis_status = "probable"
        gate_conflict_reasons: List[str] = []
        if answer_declares_insufficient and not (
            has_needs_data_subgoal or coverage_insufficient or confidence_insufficient
        ):
            gate_conflict_reasons.append("answer_declares_insufficient_evidence")
        gate_conflict = bool(gate_conflict_reasons)
        gate_decision = {
            "result": final_status,
            "reason": blocked_reason or "ok",
            "metrics": {
                "legacy_coverage": legacy_coverage_value,
                "evidence_coverage": evidence_coverage_value,
                "final_confidence": final_confidence_value,
                "needs_data_subgoal": has_needs_data_subgoal,
                "evidence_slots_missing": evidence_slots_missing,
                "coverage_insufficient": coverage_insufficient,
                "confidence_insufficient": confidence_insufficient,
                "answer_declares_insufficient": answer_declares_insufficient,
                "planning_blocked": planning_blocked,
                "ready_template_actions": ready_template_actions,
                "observed_actions": observed_actions,
                "backend_unready": backend_unready,
                "auto_exec_readonly": allow_auto_exec_readonly,
            },
            "thresholds": {
                "min_evidence_coverage": min_coverage,
                "min_final_confidence": min_final_confidence,
            },
            "missing_evidence_slots": missing_evidence_slots,
            "gate_conflict": gate_conflict,
            "gate_conflict_reasons": gate_conflict_reasons,
        }
        fault_summary = _as_str(result.get("fault_summary")).strip()
        if not fault_summary:
            fault_summary = _as_str(result.get("answer")).strip().replace("\n", " ")[:220]
        finish_summary_updates = {
            "analysis_session_id": str(result.get("analysis_session_id") or latest_run.session_id or ""),
            "conversation_id": str(result.get("conversation_id") or ""),
            "analysis_method": str(result.get("analysis_method") or ""),
            "followup_engine": str(result.get("followup_engine") or ""),
            "actions": assistant_metadata["actions"],
            "approval_required": runtime_state.get("approval_required", []),
            "react_loop": assistant_metadata["react_loop"],
            "answer_preview": str(result.get("answer") or "")[:400],
            "executed_commands": result.get("executed_commands") if isinstance(result.get("executed_commands"), list) else previous_executed_commands,
            "diagnosis_status": diagnosis_status,
            "fault_summary": fault_summary,
            "missing_evidence_slots": missing_evidence_slots,
            "next_best_commands": next_best_commands[:2],
            "gate_decision": gate_decision,
            "plan_coverage": _as_float(observe_payload.get("plan_coverage"), legacy_coverage_value),
            "exec_coverage": _as_float(observe_payload.get("exec_coverage"), -1.0),
            "evidence_coverage": evidence_coverage_value,
            "final_confidence": final_confidence_value,
            "plan_quality": plan_quality_payload,
        }
        if blocked_reason_detail:
            finish_summary_updates["blocked_reason_detail"] = blocked_reason_detail
        finish_payload = {
            "status": final_status,
            "assistant_message_id": latest_run.assistant_message_id,
            "analysis_method": str(result.get("analysis_method") or ""),
            "followup_engine": str(result.get("followup_engine") or ""),
            "action_count": len(assistant_metadata["actions"]),
            "diagnosis_status": diagnosis_status,
            "fault_summary": fault_summary,
            "gate_decision": gate_decision,
        }
        if blocked_reason:
            finish_summary_updates["blocked_reason"] = blocked_reason
            finish_payload["blocked_reason"] = blocked_reason
        if blocked_reason_detail:
            finish_payload["blocked_reason_detail"] = blocked_reason_detail
        if evidence_incomplete:
            finish_summary_updates["evidence_needs_data_subgoal"] = has_needs_data_subgoal
            if evidence_coverage_value >= 0:
                finish_summary_updates["evidence_coverage"] = evidence_coverage_value
                finish_summary_updates["evidence_coverage_threshold"] = min_coverage
            if final_confidence_value >= 0:
                finish_summary_updates["final_confidence"] = final_confidence_value
            finish_summary_updates["evidence_confidence_threshold"] = min_final_confidence
        runtime_service.finish_run(
            run_id,
            summary_updates=finish_summary_updates,
            payload=finish_payload,
            final_status=final_status,
        )
    except _RuntimePauseForPendingAction:
        # 当前 run 已进入 waiting_approval / waiting_user_input，暂停等待用户动作后再继续。
        return
    except Exception as exc:
        latest_run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
        if latest_run is None or is_terminal_run_status(latest_run.status):
            return
        logger.exception("AI runtime follow-up task failed (run_id=%s)", run_id)
        runtime_service.fail_run(
            run_id,
            error_code="followup_runtime_failed",
            error_detail=str(exc),
            summary_updates={
                "current_phase": "failed",
            },
            payload={"message": "follow-up runtime task failed"},
        )
    finally:
        latest_run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
        if latest_run is not None:
            runtime_service._update_run_summary(  # noqa: SLF001
                latest_run,
                followup_runtime_worker="idle",
                followup_runtime_last_finished_at=_utc_now_iso(),
            )


def _maybe_start_ai_run_runtime(
    runtime_service: Any,
    run: Any,
    runtime_options: Optional[Dict[str, Any]],
) -> None:
    runtime_mode = _resolve_ai_run_runtime_mode(
        getattr(run, "context_json", None),
        runtime_options,
    )
    if runtime_mode not in {"followup", "followup_analysis", "followup_runtime"}:
        return
    latest_run = runtime_service.get_run_fresh(str(getattr(run, "run_id", "") or "").strip()) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(str(getattr(run, "run_id", "") or "").strip())
    if latest_run is None or is_terminal_run_status(getattr(latest_run, "status", None)):
        return
    if _run_has_unresolved_pending_action(latest_run):
        return
    summary_payload = latest_run.summary_json if isinstance(getattr(latest_run, "summary_json", None), dict) else {}
    if _as_str(summary_payload.get("followup_runtime_worker")).strip().lower() == "running":
        return
    task = asyncio.create_task(
        _run_followup_runtime_task(
            runtime_service,
            str(getattr(run, "run_id", "") or "").strip(),
            runtime_options,
        )
    )
    runtime_service.register_background_task(str(getattr(run, "run_id", "") or "").strip(), task)


async def _resume_followup_runtime_after_active_command(
    runtime_service: Any,
    run_id: str,
    *,
    max_wait_seconds: int = 300,
    poll_interval_seconds: float = 0.3,
) -> None:
    """Wait active command finish, then resume follow-up runtime loop."""
    safe_run_id = str(run_id or "").strip()
    if not safe_run_id:
        return
    deadline = time.monotonic() + max(5, int(max_wait_seconds or 300))
    while time.monotonic() < deadline:
        run = runtime_service.get_run_fresh(safe_run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(safe_run_id)
        if run is None:
            return
        if is_terminal_run_status(getattr(run, "status", None)):
            return
        if _run_has_unresolved_pending_action(run):
            return
        status = str(getattr(run, "status", "") or "").strip().lower()
        if status in {"waiting_approval", "waiting_user_input"}:
            return
        summary = getattr(run, "summary_json", None)
        summary_dict = summary if isinstance(summary, dict) else {}
        active_command_run_id = str(summary_dict.get("active_command_run_id") or "").strip()
        if not active_command_run_id:
            runtime_options = summary_dict.get("runtime_options") if isinstance(summary_dict.get("runtime_options"), dict) else None
            _maybe_start_ai_run_runtime(runtime_service, run, runtime_options)
            return
        await asyncio.sleep(max(0.05, float(poll_interval_seconds or 0.3)))


def _schedule_followup_runtime_resume(
    runtime_service: Any,
    run_id: str,
    *,
    wait_for_active_command: bool,
) -> None:
    run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
    if run is None:
        return
    if is_terminal_run_status(getattr(run, "status", None)):
        return
    if _run_has_unresolved_pending_action(run):
        return
    if wait_for_active_command:
        task = asyncio.create_task(
            _resume_followup_runtime_after_active_command(runtime_service, run_id),
        )
        runtime_service.register_background_task(run_id, task)
        return
    summary = getattr(run, "summary_json", None)
    runtime_options = summary.get("runtime_options") if isinstance(summary, dict) and isinstance(summary.get("runtime_options"), dict) else None
    _maybe_start_ai_run_runtime(runtime_service, run, runtime_options)


def _runtime_command_requires_write(
    *,
    command: str,
    command_spec: Optional[Dict[str, Any]] = None,
) -> bool:
    safe_command = _normalize_followup_command_line(command)
    if not safe_command:
        return False
    if isinstance(command_spec, dict) and normalize_followup_command_spec(command_spec):
        compile_result = compile_followup_command_spec(command_spec)
        if bool(compile_result.get("ok")):
            safe_command = _normalize_followup_command_line(compile_result.get("command")) or safe_command
    try:
        command_meta, _ = followup_command_utils._resolve_followup_command_meta(safe_command)  # type: ignore[attr-defined]
    except Exception:
        return False
    return bool(command_meta.get("requires_write_permission")) or (
        _as_str(command_meta.get("command_type")).strip().lower() == "repair"
    )


def _merge_runtime_diagnosis_contract(*contracts: Any) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for contract in contracts:
        safe = _normalize_diagnosis_contract(contract)
        if _as_str(safe.get("fault_summary")).strip() and not _as_str(merged.get("fault_summary")).strip():
            merged["fault_summary"] = _as_str(safe.get("fault_summary")).strip()
        if _as_list(safe.get("evidence_gaps")) and not _as_list(merged.get("evidence_gaps")):
            merged["evidence_gaps"] = _as_list(safe.get("evidence_gaps"))
        if _as_list(safe.get("execution_plan")) and not _as_list(merged.get("execution_plan")):
            merged["execution_plan"] = _as_list(safe.get("execution_plan"))
        if _as_str(safe.get("why_command_needed")).strip() and not _as_str(merged.get("why_command_needed")).strip():
            merged["why_command_needed"] = _as_str(safe.get("why_command_needed")).strip()
    return _normalize_diagnosis_contract(merged)


async def _prepare_runtime_diagnosis_contract(
    *,
    run: Any,
    command: str,
    purpose: str,
    command_spec: Optional[Dict[str, Any]],
    provided_contract: Any,
) -> Dict[str, Any]:
    if not _runtime_command_requires_write(command=command, command_spec=command_spec):
        return _normalize_diagnosis_contract(provided_contract)

    summary = getattr(run, "summary_json", None) if run is not None else None
    safe_summary = summary if isinstance(summary, dict) else {}
    context = getattr(run, "context_json", None) if run is not None else None
    safe_context = context if isinstance(context, dict) else {}
    pending_command_request = (
        safe_summary.get("pending_command_request")
        if isinstance(safe_summary.get("pending_command_request"), dict)
        else {}
    )

    merged_contract = _merge_runtime_diagnosis_contract(
        provided_contract,
        command_spec.get("diagnosis_contract") if isinstance(command_spec, dict) else None,
        pending_command_request.get("diagnosis_contract"),
        safe_summary.get("diagnosis_contract"),
        safe_context.get("diagnosis_contract"),
    )
    if not _as_str(merged_contract.get("fault_summary")).strip():
        overview = _as_str(_extract_overview_summary(safe_context)).strip()
        if overview:
            merged_contract["fault_summary"] = overview
    if not _as_str(merged_contract.get("why_command_needed")).strip():
        merged_contract["why_command_needed"] = (
            _as_str(purpose).strip() or f"执行命令以补充证据并推进故障定位：{_as_str(command)[:140]}"
        )
    missing_fields = _diagnosis_contract_missing_fields(merged_contract)
    model_fill_enabled = (
        os.environ.get("PYTEST_CURRENT_TEST") is None
        and _is_truthy_env("AI_RUNTIME_DIAGNOSIS_CONTRACT_MODEL_FILL_ENABLED", True)
    )
    if missing_fields and model_fill_enabled:
        timeout_seconds = int(
            _as_float(
                ((safe_summary.get("runtime_options") or {}).get("llm_timeout_seconds") if isinstance(safe_summary.get("runtime_options"), dict) else 12),
                12,
            )
        )
        merged_contract = await _fill_diagnosis_contract_with_model(
            llm_enabled=True,
            llm_requested=True,
            timeout_seconds=max(6, timeout_seconds),
            question=_as_str(getattr(run, "question", "")),
            answer=_as_str(safe_summary.get("answer_preview")),
            analysis_context=safe_context,
            reflection=safe_summary.get("reflection") if isinstance(safe_summary.get("reflection"), dict) else {},
            actions=safe_summary.get("actions") if isinstance(safe_summary.get("actions"), list) else [],
            current_contract=merged_contract,
        )
    return _normalize_diagnosis_contract(merged_contract)


async def _create_ai_run_impl(request: AIRunCreateRequest) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    try:
        run = runtime_service.create_run(
            session_id=request.session_id,
            question=request.question,
            analysis_context=request.analysis_context,
            runtime_options=request.runtime_options,
        )
        knowledge_updates = _build_project_knowledge_summary_updates(
            run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
        )
        if knowledge_updates:
            runtime_service._update_run_summary(run, **knowledge_updates)  # noqa: SLF001
        runtime_mode = _resolve_ai_run_runtime_mode(request.analysis_context, request.runtime_options)
        if runtime_mode in {"followup", "followup_analysis", "followup_runtime"}:
            session_store = get_ai_session_store(storage)
            analysis_context = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
            await _seed_followup_runtime_history_session(
                session_store=session_store,
                run_blocking=_run_blocking,
                analysis_session_id=str(getattr(run, "session_id", "") or "").strip(),
                analysis_context=analysis_context,
                question=_mask_sensitive_text(str(getattr(run, "question", "") or "").strip()),
                user_message_id=str(getattr(run, "user_message_id", "") or "").strip(),
                conversation_id=str(getattr(run, "conversation_id", "") or "").strip(),
                extract_overview_summary=_extract_overview_summary,
                llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
                utc_now_iso=_utc_now_iso,
            )
        _maybe_start_ai_run_runtime(runtime_service, run, request.runtime_options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"run": run.to_dict()}


@router.post("/runs")
async def create_ai_run(request: AIRunCreateRequest) -> Dict[str, Any]:
    """Create a new agent runtime run."""
    _ensure_runtime_v1_api_enabled()
    return await _create_ai_run_impl(request)


async def _get_ai_run_impl(run_id: str) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    run = runtime_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run.to_dict()}


@router.get("/runs/{run_id}")
async def get_ai_run(run_id: str) -> Dict[str, Any]:
    """Get AI runtime run snapshot."""
    _ensure_runtime_v1_api_enabled()
    return await _get_ai_run_impl(run_id)


def _normalize_runtime_event_visibility(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _RUNTIME_EVENT_VISIBLE_OPTIONS:
        return normalized
    return _RUNTIME_EVENT_VISIBILITY_DEFAULT


def _is_runtime_event_visible(event_payload: Dict[str, Any], *, visibility: str) -> bool:
    if visibility == _RUNTIME_EVENT_VISIBILITY_DEBUG:
        return True
    event_type = str(event_payload.get("event_type") or "").strip().lower()
    if event_type in _RUNTIME_EVENT_DEFAULT_HIDDEN_TYPES:
        return False
    if event_type == "tool_call_progress":
        payload = event_payload.get("payload") if isinstance(event_payload.get("payload"), dict) else {}
        status = str(payload.get("status") or "").strip().lower()
        if status in {"retrying"}:
            return False
    return True


async def _get_ai_run_events_impl(
    run_id: str,
    *,
    after_seq: int,
    limit: int,
    visibility: str,
) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    run = runtime_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    safe_visibility = _normalize_runtime_event_visibility(visibility)
    events = runtime_service.list_events(run_id, after_seq=after_seq, limit=limit)
    next_after_seq = int(events[-1].seq) if events else int(after_seq)
    projected_events: List[Dict[str, Any]] = []
    for event in events:
        event_payload = event.to_dict()
        if _is_runtime_event_visible(event_payload, visibility=safe_visibility):
            projected_events.append(event_payload)
    return {
        "run_id": run_id,
        "next_after_seq": next_after_seq,
        "events": projected_events,
    }


@router.get("/runs/{run_id}/events")
async def get_ai_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    visibility: str = Query(_RUNTIME_EVENT_VISIBILITY_DEFAULT),
) -> Dict[str, Any]:
    """List AI runtime events."""
    _ensure_runtime_v1_api_enabled()
    return await _get_ai_run_events_impl(
        run_id,
        after_seq=after_seq,
        limit=limit,
        visibility=visibility,
    )


async def _stream_ai_run_impl(
    run_id: str,
    *,
    after_seq: int,
    visibility: str,
) -> StreamingResponse:
    runtime_service = get_agent_runtime_service(storage)
    run = runtime_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    safe_visibility = _normalize_runtime_event_visibility(visibility)

    async def _event_generator():
        last_seq = int(after_seq or 0)
        initial_events = runtime_service.list_events(run_id, after_seq=last_seq, limit=5000)
        for event in initial_events:
            last_seq = max(last_seq, int(event.seq))
            event_payload = event.to_dict()
            if not _is_runtime_event_visible(event_payload, visibility=safe_visibility):
                continue
            yield _format_sse_event(event.event_type, event_payload)

        current_run = runtime_service.get_run(run_id)
        if current_run is not None and is_terminal_run_status(current_run.status):
            return

        queue = runtime_service.subscribe(run_id)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    current_run = runtime_service.get_run(run_id)
                    if current_run is not None and is_terminal_run_status(current_run.status):
                        break
                    continue
                if item is None:
                    break
                if int(item.seq) <= last_seq:
                    continue
                last_seq = int(item.seq)
                item_payload = item.to_dict()
                if not _is_runtime_event_visible(item_payload, visibility=safe_visibility):
                    current_run = runtime_service.get_run(run_id)
                    if current_run is not None and is_terminal_run_status(current_run.status):
                        break
                    continue
                yield _format_sse_event(item.event_type, item_payload)
                current_run = runtime_service.get_run(run_id)
                if current_run is not None and is_terminal_run_status(current_run.status):
                    break
        finally:
            runtime_service.unsubscribe(run_id, queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_event_generator(), media_type="text/event-stream", headers=headers)


@router.get("/runs/{run_id}/stream")
async def stream_ai_run(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    visibility: str = Query(_RUNTIME_EVENT_VISIBILITY_DEFAULT),
) -> StreamingResponse:
    """Stream AI runtime events using canonical protocol."""
    _ensure_runtime_v1_api_enabled()
    return await _stream_ai_run_impl(
        run_id,
        after_seq=after_seq,
        visibility=visibility,
    )


async def _cancel_ai_run_impl(run_id: str, *, reason: str) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    await runtime_service.cancel_active_command_run(run_id)
    run = runtime_service.cancel_run(run_id, reason=reason)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run.to_dict()}


@router.post("/runs/{run_id}/cancel")
async def cancel_ai_run(run_id: str, request: AIRunCancelRequest) -> Dict[str, Any]:
    """Cancel an active AI runtime run."""
    _ensure_runtime_v1_api_enabled()
    return await _cancel_ai_run_impl(run_id, reason=request.reason)


async def _interrupt_ai_run_impl(run_id: str, *, reason: str) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    run = await runtime_service.interrupt_run(run_id, reason=reason)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run.to_dict()}


@router.post("/runs/{run_id}/interrupt")
async def interrupt_ai_run(run_id: str, request: AIRunInterruptRequest) -> Dict[str, Any]:
    """Interrupt an active run (Esc semantic)."""
    _ensure_runtime_v1_api_enabled()
    return await _interrupt_ai_run_impl(run_id, reason=request.reason)


async def _approve_ai_run_impl(run_id: str, request: AIRunApproveRequest) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    try:
        result = runtime_service.resolve_approval(
            run_id,
            approval_id=request.approval_id,
            decision=request.decision,
            comment=request.comment,
            confirmed=request.confirmed,
            elevated=request.elevated,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    idempotent_resolution = bool(result.get("idempotent"))
    resume_command_request = result.get("resume_command_request")
    command_result = None
    if isinstance(resume_command_request, dict):
        has_command_spec = bool(
            isinstance(resume_command_request.get("command_spec"), dict)
            and resume_command_request.get("command_spec")
        )
        logger.info(
            "approve_ai_run resume command: run_id=%s approval_id=%s action_id=%s has_command_spec=%s",
            run_id,
            _as_str(request.approval_id),
            _as_str(resume_command_request.get("action_id")),
            str(has_command_spec).lower(),
        )
        current_run = result.get("run")
        resume_diagnosis_contract = await _prepare_runtime_diagnosis_contract(
            run=current_run,
            command=_as_str(resume_command_request.get("command")),
            purpose=(
                _as_str(resume_command_request.get("purpose"))
                or _as_str(resume_command_request.get("title"))
                or _as_str(resume_command_request.get("command"))
            ),
            command_spec=(
                resume_command_request.get("command_spec")
                if isinstance(resume_command_request.get("command_spec"), dict)
                else None
            ),
            provided_contract=resume_command_request.get("diagnosis_contract"),
        )
        try:
            command_result = await runtime_service.execute_command_tool(
                run_id=run_id,
                action_id=_as_str(resume_command_request.get("action_id")),
                tool_call_id=_as_str(resume_command_request.get("tool_call_id")),
                command=_as_str(resume_command_request.get("command")),
                command_spec=resume_command_request.get("command_spec"),
                diagnosis_contract=resume_diagnosis_contract,
                purpose=_as_str(resume_command_request.get("purpose"))
                or _as_str(resume_command_request.get("title"))
                or _as_str(resume_command_request.get("command")),
                title=_as_str(resume_command_request.get("title")),
                tool_name=_as_str(resume_command_request.get("tool_name"), "command.exec"),
                confirmed=request.confirmed,
                elevated=request.elevated,
                confirmation_ticket=_as_str(resume_command_request.get("confirmation_ticket")),
                timeout_seconds=int(resume_command_request.get("timeout_seconds") or 20),
            )
            command_status = _as_str((command_result or {}).get("status")).strip().lower()
            auto_retry_allowed = command_status in {"confirmation_required", "elevation_required"}
            retry_reason = _as_str(
                ((command_result or {}).get("approval") or {}).get("reason")
                or ((command_result or {}).get("error") or {}).get("message")
                or ((command_result or {}).get("error") or {}).get("detail")
                or ((command_result or {}).get("error") or {}).get("reason")
            ).strip().lower()
            if auto_retry_allowed and "confirmation ticket invalid" in retry_reason:
                retry_ticket = _as_str(
                    ((command_result or {}).get("approval") or {}).get("confirmation_ticket")
                    or ((command_result or {}).get("approval") or {}).get("approval_id")
                ).strip()
                if retry_ticket:
                    retry_result = await runtime_service.execute_command_tool(
                        run_id=run_id,
                        action_id=_as_str(resume_command_request.get("action_id")),
                        tool_call_id=_as_str(resume_command_request.get("tool_call_id")),
                        command=_as_str(resume_command_request.get("command")),
                        command_spec=resume_command_request.get("command_spec"),
                        diagnosis_contract=resume_diagnosis_contract,
                        purpose=_as_str(resume_command_request.get("purpose"))
                        or _as_str(resume_command_request.get("title"))
                        or _as_str(resume_command_request.get("command")),
                        title=_as_str(resume_command_request.get("title")),
                        tool_name=_as_str(resume_command_request.get("tool_name"), "command.exec"),
                        confirmed=request.confirmed,
                        elevated=request.elevated,
                        confirmation_ticket=retry_ticket,
                        timeout_seconds=int(resume_command_request.get("timeout_seconds") or 20),
                    )
                    if isinstance(retry_result, dict):
                        retry_result["auto_retried"] = True
                    command_result = retry_result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            detail = str(exc)
            if detail == "run not found":
                raise HTTPException(status_code=404, detail=detail)
            raise HTTPException(status_code=409, detail=detail)
    response_run: Dict[str, Any]
    command_run = (command_result or {}).get("run") if isinstance(command_result, dict) else None
    if hasattr(command_run, "to_dict"):
        response_run = command_run.to_dict()
    elif isinstance(command_run, dict):
        response_run = command_run
    else:
        response_run = result["run"].to_dict()

    response: Dict[str, Any] = {
        "run": response_run,
        "approval": result["approval"],
        "command": command_result,
    }
    if idempotent_resolution:
        response["idempotent"] = True
    if isinstance(command_result, dict) and isinstance(command_result.get("approval"), dict):
        response["next_approval"] = command_result.get("approval")
    if isinstance(result.get("replan"), dict):
        response["replan"] = result["replan"]

    decision = _as_str(request.decision).strip().lower()
    if decision == "approved" and not idempotent_resolution:
        command_status = _as_str((command_result or {}).get("status")).strip().lower()
        if command_status in {"running", "running_existing"}:
            _schedule_followup_runtime_resume(
                runtime_service,
                run_id,
                wait_for_active_command=True,
            )
        elif command_status in {
            "",
            "completed",
            "executed",
            "skipped",
            "skipped_duplicate",
            "skipped_duplicate_attempt",
            "failed",
            "permission_required",
        }:
            _schedule_followup_runtime_resume(
                runtime_service,
                run_id,
                wait_for_active_command=False,
            )
        elif command_status in {"confirmation_required", "elevation_required"}:
            # 新审批已创建，run 会保持/回到 waiting_approval；无需触发 followup 继续执行。
            pass
    elif decision == "rejected" and not idempotent_resolution:
        replan_payload = result.get("replan") if isinstance(result.get("replan"), dict) else {}
        if _as_str(replan_payload.get("outcome")).strip().lower() == "replanned":
            _schedule_followup_runtime_resume(
                runtime_service,
                run_id,
                wait_for_active_command=False,
            )
    return response


@router.post("/runs/{run_id}/approve")
async def approve_ai_run(run_id: str, request: AIRunApproveRequest) -> Dict[str, Any]:
    """Resolve a pending AI runtime approval and resume the run."""
    _ensure_runtime_v1_api_enabled()
    return await _approve_ai_run_impl(run_id, request)


async def _continue_ai_run_with_user_input_impl(run_id: str, request: AIRunInputRequest) -> Dict[str, Any]:
    runtime_service = get_agent_runtime_service(storage)
    await ensure_runtime_input_context_ready(run_id)
    try:
        result = runtime_service.submit_user_input(
            run_id,
            text=request.text,
            source=request.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="run not found")
    run = result.get("run")
    if run is not None and _as_str(getattr(run, "status", "")).strip().lower() == "running":
        _schedule_followup_runtime_resume(
            runtime_service,
            run_id,
            wait_for_active_command=False,
        )
    return {
        "run": result["run"].to_dict(),
        "user_input": result.get("user_input", {}),
    }


@router.post("/runs/{run_id}/input")
async def continue_ai_run_with_user_input(run_id: str, request: AIRunInputRequest) -> Dict[str, Any]:
    """Continue a waiting_user_input run with one-line user input."""
    _ensure_runtime_v1_api_enabled()
    return await _continue_ai_run_with_user_input_impl(run_id, request)


async def execute_ai_run_command(run_id: str, request: AIRunCommandRequest) -> Dict[str, Any]:
    """Execute a command as a tool call inside an AI runtime run.

    Internal entrypoint for runtime v4 bridge; v1 HTTP route is retired.
    """
    runtime_service = get_agent_runtime_service(storage)
    safe_purpose = _as_str(request.purpose).strip()
    if not safe_purpose:
        raise HTTPException(status_code=400, detail="purpose is required")
    structured_required = True
    request_command_spec = request.command_spec if isinstance(request.command_spec, dict) else {}
    safe_command_spec = normalize_followup_command_spec(request_command_spec)
    _metric_inc(AI_RUNTIME_COMMAND_ACTION_REQUEST_TOTAL)
    if safe_command_spec:
        _metric_inc(AI_RUNTIME_STRUCTURED_SPEC_PRESENT_TOTAL)
    command_for_contract = _as_str(request.command)
    command_spec_for_exec: Optional[Dict[str, Any]] = request_command_spec
    if structured_required:
        if not safe_command_spec:
            _metric_inc(AI_RUNTIME_BLOCKED_MISSING_SPEC_TOTAL)
            run = runtime_service.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            recovery_payload = build_command_spec_self_repair_payload(
                reason="missing_or_invalid_command_spec",
                detail="missing_or_invalid_command_spec: command_spec is required",
                command_spec=request_command_spec,
                raw_command=_as_str(request.command),
            )
            return {
                "status": "blocked",
                "tool_call_id": "",
                "run": run.to_dict(),
                "error": {
                    "code": "missing_or_invalid_command_spec",
                    "message": "missing_or_invalid_command_spec: command_spec is required",
                    "recovery": recovery_payload,
                },
                "recovery": recovery_payload,
            }
        compile_result = compile_followup_command_spec(safe_command_spec, run_sql_preflight=True)
        if not bool(compile_result.get("ok")):
            if _as_str(compile_result.get("reason")).strip().lower() == "sql_preflight_failed":
                _metric_inc(AI_RUNTIME_SQL_PREFLIGHT_FAIL_TOTAL)
            _metric_inc(AI_RUNTIME_BLOCKED_MISSING_SPEC_TOTAL)
            run = runtime_service.get_run(run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            compile_reason = _as_str(compile_result.get("reason"), "compile failed")
            compile_detail = _as_str(compile_result.get("detail")).strip()
            message = (
                f"invalid command_spec: {compile_reason}"
                if not compile_detail
                else f"invalid command_spec: {compile_reason}: {compile_detail}"
            )
            recovery_payload = build_command_spec_self_repair_payload(
                reason=compile_reason,
                detail=compile_detail or compile_reason,
                command_spec=safe_command_spec,
                raw_command=_as_str(request.command),
            )
            return {
                "status": "blocked",
                "tool_call_id": "",
                "run": run.to_dict(),
                "error": {
                    "code": "missing_or_invalid_command_spec",
                    "message": message,
                    "recovery": recovery_payload,
                },
                "recovery": recovery_payload,
            }
        normalized_spec = (
            compile_result.get("command_spec")
            if isinstance(compile_result.get("command_spec"), dict)
            else safe_command_spec
        )
        command_spec_for_exec = normalized_spec
        command_for_contract = _as_str(compile_result.get("command"))
    current_run = runtime_service.get_run(run_id)
    prepared_contract = await _prepare_runtime_diagnosis_contract(
        run=current_run,
        command=command_for_contract,
        purpose=safe_purpose,
        command_spec=command_spec_for_exec if isinstance(command_spec_for_exec, dict) else None,
        provided_contract=request.diagnosis_contract,
    )
    try:
        approval_token = _as_str(request.approval_token).strip()
        result = await runtime_service.execute_command_tool(
            run_id=run_id,
            action_id=request.action_id,
            tool_call_id="",
            command=command_for_contract,
            command_spec=command_spec_for_exec,
            diagnosis_contract=prepared_contract,
            purpose=safe_purpose,
            title=request.title,
            tool_name=request.tool_name,
            confirmed=request.confirmed,
            elevated=request.elevated,
            confirmation_ticket=approval_token,
            timeout_seconds=request.timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        detail = str(exc)
        if detail == "run not found":
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=409, detail=detail)
    response: Dict[str, Any] = {
        "status": _as_str(result.get("status"), "failed"),
        "tool_call_id": _as_str(result.get("tool_call_id")),
    }
    if _as_str(result.get("command_run_id")):
        response["command_run_id"] = _as_str(result.get("command_run_id"))
    run = result.get("run")
    if hasattr(run, "to_dict"):
        response["run"] = run.to_dict()
    elif isinstance(run, dict):
        response["run"] = run
    if isinstance(result.get("command_run"), dict):
        response["command_run"] = result["command_run"]
    if isinstance(result.get("approval"), dict):
        response["approval"] = result["approval"]
    if isinstance(result.get("error"), dict):
        response["error"] = result["error"]
    return response


async def _run_blocking(func, *args, **kwargs):
    """在线程池执行阻塞 IO，避免阻塞事件循环。"""
    # 在 pytest 进程中，to_thread 在当前环境会出现退出卡死，测试时走同步路径。
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return func(*args, **kwargs)
    return await asyncio.to_thread(func, *args, **kwargs)


def _resolve_runtime_input_context_hydrate_timeout_seconds() -> int:
    return max(
        5,
        min(
            300,
            int(
                _as_float(
                    os.getenv("AI_RUNTIME_INPUT_CONTEXT_HYDRATE_TIMEOUT_SECONDS"),
                    60,
                )
            ),
        ),
    )


def _resolve_runtime_input_context_hydrate_retry_max() -> int:
    return max(
        0,
        min(
            3,
            int(
                _as_float(
                    os.getenv("AI_RUNTIME_INPUT_CONTEXT_HYDRATE_RETRY_MAX"),
                    1,
                )
            ),
        ),
    )


def _stabilize_followup_answer_when_plan_is_non_executable(
    *,
    answer: str,
    actions: List[Dict[str, Any]],
    action_observations: List[Dict[str, Any]],
    react_loop: Dict[str, Any],
) -> str:
    """避免在无可执行动作/无观察事实时输出“已生成命令”的误导性文案。"""
    safe_answer = _as_str(answer).strip()
    if not safe_answer:
        return safe_answer

    safe_loop = react_loop if isinstance(react_loop, dict) else {}
    plan = safe_loop.get("plan") if isinstance(safe_loop.get("plan"), dict) else {}
    execute = safe_loop.get("execute") if isinstance(safe_loop.get("execute"), dict) else {}
    observed = int(_as_float(execute.get("observed_actions"), 0))
    executable_actions = int(_as_float(plan.get("executable_actions"), 0))
    if observed > 0 or executable_actions > 0:
        return safe_answer

    safe_actions = [item for item in _as_list(actions) if isinstance(item, dict)]
    safe_observations = [item for item in _as_list(action_observations) if isinstance(item, dict)]
    if not safe_actions and safe_observations:
        return safe_answer

    normalized = safe_answer
    normalized = normalized.replace(
        "已生成相应的只读查询命令来收集这些证据。",
        "当前还没有生成通过校验的结构化查询命令，需要先补足证据或补全 command_spec。",
    )
    normalized = normalized.replace(
        "已生成可执行动作建议。",
        "当前只生成了待验证动作草稿，尚未形成可执行命令。",
    )
    if "执行步骤：" in normalized:
        normalized = normalized.replace(
            "执行步骤：",
            (
                "执行步骤：\n"
                "- 当前未生成通过校验的结构化命令，以下项目仅是待验证排查草稿，"
                "不能视为系统已能执行的步骤。"
            ),
            1,
        )
    warning = (
        "说明：当前没有生成通过校验的结构化命令，也没有自动执行任何观察动作。"
        "下面的结论与步骤仍是待证据验证的诊断草稿。"
    )
    if warning not in normalized:
        normalized = f"{warning}\n\n{normalized}"
    return normalized


def _build_runtime_input_context_error_detail(
    *,
    code: str,
    message: str,
    timeout_seconds: int,
    attempts: int,
    reason: str = "",
) -> Dict[str, Any]:
    return {
        "code": _as_str(code, "context_hydration_failed"),
        "message": _as_str(message, "载入会话上下文失败，请重试。"),
        "retryable": True,
        "timeout_seconds": int(timeout_seconds),
        "attempts": int(attempts),
        "reason": _as_str(reason),
    }


def _runtime_run_requires_context_hydration(run: Any) -> bool:
    safe_context = getattr(run, "context_json", None)
    safe_summary = getattr(run, "summary_json", None)
    runtime_options = (
        safe_summary.get("runtime_options")
        if isinstance(safe_summary, dict) and isinstance(safe_summary.get("runtime_options"), dict)
        else {}
    )
    mode = _resolve_ai_run_runtime_mode(
        safe_context if isinstance(safe_context, dict) else {},
        runtime_options if isinstance(runtime_options, dict) else {},
    )
    return mode in {"followup", "followup_analysis", "followup_runtime"}


async def _hydrate_runtime_input_context_once(
    *,
    runtime_service: Any,
    run_id: str,
) -> Dict[str, Any]:
    run = runtime_service.get_run_fresh(run_id) if hasattr(runtime_service, "get_run_fresh") else runtime_service.get_run(run_id)
    if run is None:
        raise RuntimeError("run_not_found")
    if not _runtime_run_requires_context_hydration(run):
        return {"status": "skipped", "reason": "runtime_mode_not_followup"}

    summary_payload = run.summary_json if isinstance(getattr(run, "summary_json", None), dict) else {}
    runtime_options = summary_payload.get("runtime_options") if isinstance(summary_payload.get("runtime_options"), dict) else {}
    context_payload = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}

    analysis_session_id = _as_str(getattr(run, "session_id", "")).strip()
    if not analysis_session_id:
        analysis_session_id = _as_str(summary_payload.get("analysis_session_id")).strip()
    if not analysis_session_id:
        raise RuntimeError("analysis_session_id_missing")

    conversation_id = _as_str(getattr(run, "conversation_id", "")).strip()
    if not conversation_id:
        conversation_id = _as_str(runtime_options.get("conversation_id")).strip()
    if not conversation_id:
        conversation_id = _as_str(context_payload.get("conversation_id")).strip()

    session_store = get_ai_session_store(storage)
    existing_session = await _run_blocking(session_store.get_session, analysis_session_id)
    if existing_session is None:
        raise RuntimeError("analysis_session_not_found")

    cached_history = _get_conversation_history(conversation_id) if conversation_id else []
    stored_history = await _build_followup_history(
        session_store=session_store,
        run_blocking=_run_blocking,
        analysis_session_id=analysis_session_id,
        request_history=[],
        conversation_id="",
        normalize_conversation_history=_normalize_conversation_history,
        mask_sensitive_payload=_mask_sensitive_payload,
        get_conversation_history=lambda _conversation_id: [],
        session_messages_to_conversation_history=_session_messages_to_conversation_history,
        merge_conversation_history=_merge_conversation_history,
    )
    merged_history = _merge_conversation_history(stored_history, cached_history, max_items=40)
    if conversation_id:
        _set_conversation_history(
            conversation_id,
            _trim_conversation_history(merged_history, max_items=40),
        )

    return {
        "status": "ok",
        "analysis_session_id": analysis_session_id,
        "conversation_id": conversation_id,
        "history_items": len(merged_history),
    }


async def ensure_runtime_input_context_ready(run_id: str) -> Dict[str, Any]:
    """输入继续前，强制回灌当前会话上下文（隐形步骤）。"""
    safe_run_id = _as_str(run_id).strip()
    if not safe_run_id:
        raise HTTPException(status_code=400, detail="run_id is required")

    runtime_service = get_agent_runtime_service(storage)
    existing_run = (
        runtime_service.get_run_fresh(safe_run_id)
        if hasattr(runtime_service, "get_run_fresh")
        else runtime_service.get_run(safe_run_id)
    )
    if existing_run is None:
        raise HTTPException(status_code=404, detail="run not found")

    if not _runtime_run_requires_context_hydration(existing_run):
        return {"status": "skipped", "reason": "runtime_mode_not_followup"}

    timeout_seconds = _resolve_runtime_input_context_hydrate_timeout_seconds()
    retry_max = _resolve_runtime_input_context_hydrate_retry_max()
    total_attempts = retry_max + 1
    last_reason = ""

    for attempt in range(1, total_attempts + 1):
        try:
            result = await asyncio.wait_for(
                _hydrate_runtime_input_context_once(
                    runtime_service=runtime_service,
                    run_id=safe_run_id,
                ),
                timeout=float(timeout_seconds),
            )
            return {
                "status": "ok",
                "attempt": attempt,
                "timeout_seconds": timeout_seconds,
                "result": result if isinstance(result, dict) else {},
            }
        except asyncio.TimeoutError:
            last_reason = "timeout"
            logger.warning(
                "Runtime input context hydration timed out (run_id=%s, attempt=%s/%s, timeout=%ss)",
                safe_run_id,
                attempt,
                total_attempts,
                timeout_seconds,
            )
        except Exception as exc:
            last_reason = _as_str(exc) or exc.__class__.__name__
            logger.warning(
                "Runtime input context hydration failed (run_id=%s, attempt=%s/%s): %s",
                safe_run_id,
                attempt,
                total_attempts,
                exc,
            )
        if attempt < total_attempts:
            await asyncio.sleep(min(1.0, 0.25 * attempt))

    if last_reason == "timeout":
        raise HTTPException(
            status_code=409,
            detail=_build_runtime_input_context_error_detail(
                code="context_hydration_timeout",
                message=(
                    f"载入当前会话上下文超时（{timeout_seconds}s，已重试 {total_attempts} 次），"
                    "本次输入已阻断，请点击重试。"
                ),
                timeout_seconds=timeout_seconds,
                attempts=total_attempts,
                reason=last_reason,
            ),
        )
    raise HTTPException(
        status_code=409,
        detail=_build_runtime_input_context_error_detail(
            code="context_hydration_failed",
            message="载入当前会话上下文失败，本次输入已阻断，请点击重试。",
            timeout_seconds=timeout_seconds,
            attempts=total_attempts,
            reason=last_reason,
        ),
    )


def _utc_now_iso() -> str:
    """返回 UTC ISO8601 时间（以 Z 结尾）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _prune_conversation_sessions(now_ts: Optional[float] = None) -> None:
    """按 TTL + 容量淘汰会话缓存，避免内存无界增长。"""
    current_ts = now_ts if now_ts is not None else time.time()

    expired_keys: List[str] = []
    for conversation_id, payload in _conversation_sessions.items():
        updated_at = float(payload.get("updated_at", 0.0))
        if updated_at > 0 and (current_ts - updated_at) > AI_FOLLOWUP_SESSION_CACHE_TTL_SECONDS:
            expired_keys.append(conversation_id)

    for conversation_id in expired_keys:
        _conversation_sessions.pop(conversation_id, None)

    while len(_conversation_sessions) > AI_FOLLOWUP_SESSION_CACHE_MAX:
        _conversation_sessions.popitem(last=False)


def _get_conversation_history(conversation_id: str) -> List[Dict[str, Any]]:
    """读取会话缓存并刷新 LRU 顺序。"""
    _prune_conversation_sessions()
    payload = _conversation_sessions.get(conversation_id)
    if not isinstance(payload, dict):
        return []
    history = payload.get("history")
    if not isinstance(history, list):
        history = []
    payload["updated_at"] = time.time()
    _conversation_sessions[conversation_id] = payload
    _conversation_sessions.move_to_end(conversation_id)
    return history


def _set_conversation_history(conversation_id: str, history: List[Dict[str, Any]]) -> None:
    """写入会话缓存并执行淘汰。"""
    _conversation_sessions[conversation_id] = {
        "history": history,
        "updated_at": time.time(),
    }
    _conversation_sessions.move_to_end(conversation_id)
    _prune_conversation_sessions()


def _clear_conversation_history(conversation_id: str) -> None:
    """清理单个会话缓存。"""
    _conversation_sessions.pop(conversation_id, None)


def _is_llm_configured() -> bool:
    """判断 LLM 运行所需配置是否可用。"""
    provider = (os.getenv("LLM_PROVIDER", "openai") or "openai").strip().lower()

    if provider == "local":
        return bool(
            os.getenv("LLM_API_KEY")
            or os.getenv("LOCAL_MODEL_API_KEY")
            or os.getenv("LOCAL_MODEL_API_BASE")
            or os.getenv("LOCAL_MODEL_BASE_URL")
            or os.getenv("LOCAL_MODEL_PATH")
        )

    return bool(
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )


def _resolve_followup_engine() -> str:
    """解析追问引擎开关。"""
    engine = _as_str(os.getenv("AI_FOLLOWUP_ENGINE"), "legacy").strip().lower()
    if engine == "langchain":
        return "langchain"
    return "legacy"


def _build_llm_runtime_status() -> Dict[str, Any]:
    """返回当前 LLM 运行时配置状态（供后续本地 LLM 接入扩展）。"""
    provider = (os.getenv("LLM_PROVIDER", "openai") or "openai").strip().lower()
    model = (os.getenv("LLM_MODEL", "") or "").strip()
    local_api_base = (
        os.getenv("LOCAL_MODEL_API_BASE")
        or os.getenv("LOCAL_MODEL_BASE_URL")
        or os.getenv("LLM_API_BASE")
        or ""
    ).strip()

    deployment_file = _resolve_llm_deployment_file_path({})
    deployment_exists = os.path.exists(deployment_file)
    deployment_writable = deployment_exists and os.access(deployment_file, os.W_OK)

    return {
        "configured_provider": provider,
        "configured_model": model,
        "llm_enabled": _is_llm_configured(),
        "api_key_configured": bool(
            os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("LOCAL_MODEL_API_KEY")
        ),
        "local_llm_ready": bool(local_api_base),
        "local_llm_api_base": local_api_base,
        "supported_providers": ["openai", "claude", "deepseek", "local"],
        "runtime_config_contract": {
            "provider": "openai|claude|deepseek|local",
            "model": "string",
            "api_base": "string(url)",
            "api_key": "string(optional, masked input)",
            "local_model_path": "string(optional)",
            "persist_to_deployment": "bool(default=true)",
            "extra": "object(optional)",
        },
        "deployment_persistence": {
            "deployment_file": deployment_file,
            "deployment_file_exists": deployment_exists,
            "deployment_file_writable": deployment_writable,
            "enabled_by_default": True,
        },
        "note": (
            "支持通过 /api/v1/ai/llm/runtime/update 更新运行时配置，并尝试同步写入部署文件；"
            "当部署文件不可访问时，仅当前进程生效。"
        ),
    }


def _build_kb_runtime_status(force_refresh_provider_status: bool = False) -> Dict[str, Any]:
    provider = _normalize_kb_provider_name(_as_str(os.getenv("KB_REMOTE_PROVIDER"), "ragflow"))
    defaults = _kb_provider_defaults(provider)
    base_url = _as_str(
        os.getenv("KB_REMOTE_BASE_URL")
        or (os.getenv("KB_RAGFLOW_BASE_URL") if provider == "ragflow" else "")
    )
    api_key_configured = bool(
        os.getenv("KB_REMOTE_API_KEY")
        or (os.getenv("KB_RAGFLOW_API_KEY") if provider == "ragflow" else "")
    )
    dataset_id = _as_str(
        os.getenv("KB_RAGFLOW_DATASET_ID") if provider == "ragflow" else os.getenv("KB_REMOTE_DATASET_ID")
    )
    timeout_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_TIMEOUT_SECONDS"), 5)))
    health_path = _as_str(os.getenv("KB_REMOTE_HEALTH_PATH"), defaults["health_path"])
    search_path = _as_str(os.getenv("KB_REMOTE_SEARCH_PATH"), defaults["search_path"])
    upsert_path = _as_str(os.getenv("KB_REMOTE_UPSERT_PATH"), defaults["upsert_path"])
    outbox_enabled = _as_str(os.getenv("KB_REMOTE_OUTBOX_ENABLED"), "true").lower() == "true"
    outbox_poll_seconds = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_POLL_SECONDS"), 5)))
    outbox_max_attempts = max(1, int(_as_float(os.getenv("KB_REMOTE_OUTBOX_MAX_ATTEMPTS"), 5)))

    provider_status: Dict[str, Any] = {
        "remote_available": False,
        "remote_configured": bool(base_url) and provider != "disabled" and (provider != "ragflow" or bool(dataset_id)),
        "message": "provider status unavailable",
    }
    try:
        gateway = get_knowledge_gateway(storage)
        provider_status = gateway.get_provider_status(force_refresh=force_refresh_provider_status)
    except Exception as exc:
        provider_status["message"] = f"provider status unavailable: {exc}"

    deployment_file = _resolve_kb_deployment_file_path({})
    deployment_exists = os.path.exists(deployment_file)
    deployment_writable = deployment_exists and os.access(deployment_file, os.W_OK)

    return {
        "configured_provider": provider,
        "configured_base_url": base_url,
        "api_key_configured": api_key_configured,
        "configured_dataset_id": dataset_id,
        "timeout_seconds": timeout_seconds,
        "health_path": health_path,
        "search_path": search_path,
        "upsert_path": upsert_path,
        "outbox_enabled": outbox_enabled,
        "outbox_poll_seconds": outbox_poll_seconds,
        "outbox_max_attempts": outbox_max_attempts,
        "supported_providers": ["ragflow", "generic_rest", "disabled"],
        "runtime_config_contract": {
            "provider": "ragflow|generic_rest|disabled",
            "base_url": "string(url)",
            "api_key": "string(optional, masked input)",
            "dataset_id": "string(required when provider=ragflow)",
            "timeout_seconds": "int(default=5)",
            "health_path": "string(path)",
            "search_path": "string(path)",
            "upsert_path": "string(path)",
            "outbox_enabled": "bool(default=true)",
            "outbox_poll_seconds": "int(default=5)",
            "outbox_max_attempts": "int(default=5)",
            "persist_to_deployment": "bool(default=true)",
            "extra": "object(optional)",
        },
        "provider_status": provider_status,
        "deployment_persistence": {
            "deployment_file": deployment_file,
            "deployment_file_exists": deployment_exists,
            "deployment_file_writable": deployment_writable,
            "enabled_by_default": True,
        },
        "note": (
            "默认支持 RAGFlow provider，可通过 /api/v1/ai/kb/runtime/update 在线更新。"
            "RAGFlow 需显式配置 dataset_id，并按原生 datasets/documents API 同步。"
            "保存后会重建 KB 网关以立即生效。"
        ),
    }


class AnalyzeLogRequest(BaseModel):
    """单条日志分析请求"""
    id: str
    timestamp: str
    entity: Dict[str, Any]
    event: Dict[str, Any]
    context: Dict[str, Any] = {}


class AnalyzeTraceRequest(BaseModel):
    """链路分析请求"""
    trace_id: str


class LLMAnalyzeRequest(BaseModel):
    """LLM 分析请求"""
    log_content: str
    service_name: str = ""
    context: Dict[str, Any] = None
    use_llm: bool = True
    enable_agent: bool = True
    enable_web_search: bool = False


class LLMTraceAnalyzeRequest(BaseModel):
    """LLM 链路分析请求"""
    trace_id: str
    service_name: str = ""


class LLMRuntimeConfig(BaseModel):
    """LLM 运行时配置（预留扩展接口）"""
    provider: Optional[str] = None
    model: Optional[str] = None
    api_base: Optional[str] = None
    local_model_path: Optional[str] = None
    extra: Dict[str, Any] = {}


class LLMRuntimeUpdateRequest(BaseModel):
    """LLM 运行时更新请求（支持 API key 动态更新）"""
    provider: Optional[str] = None
    model: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    local_model_path: Optional[str] = None
    clear_api_key: bool = False
    persist_to_deployment: bool = True
    extra: Dict[str, Any] = {}


class KBRemoteRuntimeConfig(BaseModel):
    """远端知识库运行时配置请求（RAGFlow/Generic REST）。"""
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    dataset_id: Optional[str] = None
    timeout_seconds: Optional[int] = None
    health_path: Optional[str] = None
    search_path: Optional[str] = None
    upsert_path: Optional[str] = None
    outbox_enabled: Optional[bool] = None
    outbox_poll_seconds: Optional[int] = None
    outbox_max_attempts: Optional[int] = None
    clear_api_key: bool = False
    persist_to_deployment: bool = True
    extra: Dict[str, Any] = {}


def _as_str(value: Any, default: str = "") -> str:
    """将任意值转为字符串。"""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_float(value: Any, default: float = 0.0) -> float:
    """将任意值转为浮点数。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[Any]:
    """确保返回列表。"""
    return value if isinstance(value, list) else []


def _build_followup_fallback_answer(
    question: str,
    analysis_context: Dict[str, Any],
    fallback_reason: str = "llm_unavailable",
    reflection: Optional[Dict[str, Any]] = None,
) -> str:
    """
    LLM 不可用时的规则降级回答。
    尽量基于当前分析上下文给出可执行建议。
    """
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    overview = result.get("overview") if isinstance(result, dict) else {}
    problem = _as_str(overview.get("problem"), "unknown")
    severity = _as_str(overview.get("severity"), "unknown")
    summary = _as_str(overview.get("description"), "暂无摘要")
    trace_id = _as_str(analysis_context.get("trace_id"))
    service_name = _as_str(analysis_context.get("service_name"), "unknown")
    sql_perf_context_text = " ".join(
        [
            _as_str(question).lower(),
            _as_str(problem).lower(),
            _as_str(summary).lower(),
        ]
    )
    is_sql_performance_context = any(
        token in sql_perf_context_text
        for token in (
            "ch_query_slow",
            "slow query",
            "clickhouse",
            "sql",
            "prewhere",
            "慢查询",
            "查询慢",
            "耗时",
            "延迟",
            "latency",
            "performance",
        )
    )

    hints: List[str] = [
        f"当前上下文问题类型: {problem}",
        f"严重级别: {severity}",
        f"服务: {service_name}",
        f"摘要: {summary}",
    ]
    if trace_id:
        hints.append(f"trace_id: {trace_id}")

    reason_text = "当前处于规则模式（LLM 不可用）"
    if fallback_reason == "llm_disabled_by_user":
        reason_text = "当前处于规则模式（已关闭 LLM 开关）"
    elif fallback_reason == "llm_timeout":
        reason_text = "当前处于规则模式（LLM 响应超时，已自动降级）"

    suggestion_lines: List[str] = []
    reflection_dict = reflection if isinstance(reflection, dict) else {}
    for action in _as_list(reflection_dict.get("next_actions"))[:3]:
        action_text = _as_str(action)
        if action_text:
            suggestion_lines.append(f"- {action_text}")

    suggestion_text = (
        "建议：先补齐慢查询关键证据（目标 SQL、EXPLAIN、表结构、时间窗口）后继续追问。"
        if is_sql_performance_context
        else "建议：先按根因列表逐项验证，并补充关键证据（日志/trace/指标）后继续追问。"
    )
    base_answer = (
        f"{reason_text}，已结合上下文给出建议。\n"
        f"你的追问：{question}\n"
        + "\n".join(f"- {line}" for line in hints)
        + f"\n{suggestion_text}"
    )
    if suggestion_lines:
        return f"{base_answer}\n建议补齐证据：\n" + "\n".join(suggestion_lines)
    return base_answer


def _build_case_analysis_result(case_obj: Any) -> Dict[str, Any]:
    """将案例对象还原为 AIAnalysis 可直接渲染的统一结构。"""
    raw_llm_metadata = getattr(case_obj, "llm_metadata", {})
    llm_metadata = raw_llm_metadata if isinstance(raw_llm_metadata, dict) else {}
    raw_result = {
        "problem_type": case_obj.problem_type,
        "severity": case_obj.severity,
        "summary": case_obj.summary,
        "confidence": llm_metadata.get("confidence", 0.0),
        "root_causes": case_obj.root_causes or [],
        "solutions": case_obj.solutions or [],
        "similar_cases": llm_metadata.get("similar_cases", []),
    }
    return _normalize_analysis_result(
        raw_result,
        analysis_method=_as_str(llm_metadata.get("analysis_method"), "history"),
        fallback_description=case_obj.summary,
    )


def _get_case_status(case_obj: Any) -> str:
    """读取案例状态。"""
    raw_llm_metadata = getattr(case_obj, "llm_metadata", {})
    llm_metadata = raw_llm_metadata if isinstance(raw_llm_metadata, dict) else {}
    status = _as_str(llm_metadata.get("case_status")).lower()
    if status:
        return status
    return "resolved" if bool(case_obj.resolved) else "archived"


def _collect_root_causes_from_result(result: Dict[str, Any]) -> List[str]:
    """从统一分析结果提取根因标题。"""
    root_causes = result.get("rootCauses") if isinstance(result, dict) else []
    items: List[str] = []
    for cause in _as_list(root_causes):
        if isinstance(cause, dict):
            title = _as_str(cause.get("title") or cause.get("description"))
            if title:
                items.append(title)
        else:
            text = _as_str(cause)
            if text:
                items.append(text)
    return items


def _collect_manual_remediation_steps_from_messages(messages: List[Any]) -> List[str]:
    """从追问助手回复提取候选步骤。"""
    steps: List[str] = []
    for msg in messages:
        role = _as_str(getattr(msg, "role", "") if not isinstance(msg, dict) else msg.get("role")).lower()
        if role != "assistant":
            continue
        content = _as_str(getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content"))
        if not content:
            continue
        for line in content.splitlines():
            text = line.strip().lstrip("-").lstrip("*").strip()
            if len(text) >= 8:
                steps.append(text)
    unique_steps: List[str] = []
    seen = set()
    for step in steps:
        if step not in seen:
            seen.add(step)
            unique_steps.append(step)
    return unique_steps[:8]


def _truncate_text(value: Any, max_len: int) -> str:
    """裁剪文本长度，避免 prompt 与返回字段膨胀。"""
    text = _as_str(value)
    if max_len <= 0:
        return ""
    return text[:max_len]


def _normalize_kb_draft_severity(value: Any, default: str = "medium") -> str:
    """规范化严重级别。"""
    severity = _as_str(value, default).strip().lower()
    aliases = {
        "sev0": "critical",
        "sev1": "high",
        "sev2": "medium",
        "sev3": "low",
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
    }
    normalized = aliases.get(severity, severity)
    if normalized not in {"critical", "high", "medium", "low", "unknown"}:
        return default
    return normalized


def _normalize_string_list(raw: Any, max_items: int = 8, min_length: int = 4) -> List[str]:
    """将任意列表归一化为去重字符串列表。"""
    items: List[str] = []
    for item in _as_list(raw):
        text = _as_str(item).strip().lstrip("-").lstrip("*").strip()
        if len(text) >= min_length:
            items.append(text)

    unique_items: List[str] = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
    return unique_items[:max(1, max_items)]


def _build_rule_based_kb_draft(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
) -> Dict[str, Any]:
    """生成规则模式草稿，作为默认路径与 LLM 回退底稿。"""
    result_container = session.get("result") if isinstance(session, dict) else {}
    raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
    normalized = _normalize_analysis_result(raw_result, fallback_description=_as_str(session.get("summary_text")))
    overview = normalized.get("overview") if isinstance(normalized.get("overview"), dict) else {}
    summary = _as_str(overview.get("description"), _as_str(session.get("summary_text")))
    problem_type = _as_str(overview.get("problem"), "unknown").lower()
    severity = _normalize_kb_draft_severity(overview.get("severity"), default="medium")
    root_causes = _collect_root_causes_from_result(normalized)
    solutions = _normalize_solutions(normalized.get("solutions"))
    remediation_steps = (
        _collect_manual_remediation_steps_from_messages(messages if bool(include_followup) else [])
        if bool(include_followup)
        else []
    )

    return {
        "problem_type": problem_type,
        "severity": severity,
        "summary": summary,
        "log_content": _as_str(session.get("input_text")),
        "service_name": _as_str(session.get("service_name")),
        "root_causes": root_causes,
        "solutions": solutions,
        "analysis_summary": summary,
        "manual_remediation_steps": remediation_steps,
    }


def _build_kb_draft_quality(
    draft_case: Dict[str, Any],
    confidence_hint: Optional[float] = None,
) -> Tuple[List[str], float]:
    """评估草稿必填项完整度并给出置信度。"""
    missing_required_fields: List[str] = []
    for key in ["problem_type", "severity", "summary", "log_content", "service_name"]:
        if not _as_str(draft_case.get(key)):
            missing_required_fields.append(key)

    if not _normalize_string_list(draft_case.get("root_causes"), max_items=8, min_length=2):
        missing_required_fields.append("root_causes")

    normalized_solutions = _normalize_solutions(draft_case.get("solutions"))
    if not normalized_solutions:
        missing_required_fields.append("solutions")
    draft_case["solutions"] = normalized_solutions

    if confidence_hint is None:
        confidence = 0.86
        if missing_required_fields:
            confidence = 0.62
        elif _as_str(draft_case.get("problem_type"), "unknown").lower() == "unknown":
            confidence = 0.71
    else:
        confidence = max(0.0, min(float(confidence_hint), 1.0))
        if missing_required_fields:
            confidence = min(confidence, 0.65)

    draft_case["root_causes"] = _normalize_string_list(draft_case.get("root_causes"), max_items=8, min_length=2)
    draft_case["manual_remediation_steps"] = _normalize_string_list(
        draft_case.get("manual_remediation_steps"),
        max_items=8,
        min_length=4,
    )
    draft_case["summary"] = _truncate_text(_as_str(draft_case.get("summary")), 1000)
    draft_case["analysis_summary"] = _truncate_text(
        _as_str(draft_case.get("analysis_summary") or draft_case.get("summary")),
        1200,
    )
    draft_case["problem_type"] = _as_str(draft_case.get("problem_type"), "unknown").lower()
    draft_case["severity"] = _normalize_kb_draft_severity(draft_case.get("severity"), default="medium")
    draft_case["log_content"] = _truncate_text(_as_str(draft_case.get("log_content")), 8000)
    draft_case["service_name"] = _as_str(draft_case.get("service_name"))

    return missing_required_fields, confidence


def _build_kb_conversation_transcript(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
    normalized_result: Dict[str, Any],
) -> str:
    """构建会话文本摘要，供 LLM 进行全会话归纳。"""
    session_id = _as_str(session.get("session_id"))
    analysis_type = _as_str(session.get("analysis_type"), "log")
    service_name = _as_str(session.get("service_name"), "unknown")
    trace_id = _as_str(session.get("trace_id"))
    summary_text = _as_str(session.get("summary_text"))
    input_text = _truncate_text(_mask_sensitive_text(_as_str(session.get("input_text"))), 2000)

    max_messages = max(20, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_MAX_MESSAGES", 120), 120)))
    max_message_chars = max(120, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_MAX_MESSAGE_CHARS", 900), 900)))
    selected_messages = messages if include_followup else []
    selected_messages = _as_list(selected_messages)[-max_messages:]

    dialogue_lines: List[str] = []
    for msg in selected_messages:
        role = _as_str(msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")).lower()
        content = _as_str(msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", ""))
        if role not in {"user", "assistant"} or not content:
            continue
        safe_content = _truncate_text(_mask_sensitive_text(content), max_message_chars)
        dialogue_lines.append(f"{role}: {safe_content}")

    overview = normalized_result.get("overview") if isinstance(normalized_result, dict) else {}
    overview_problem = _as_str(overview.get("problem"), "unknown") if isinstance(overview, dict) else "unknown"
    overview_severity = _as_str(overview.get("severity"), "unknown") if isinstance(overview, dict) else "unknown"
    overview_description = _as_str(overview.get("description"))
    root_causes = _collect_root_causes_from_result(normalized_result)
    solutions = _normalize_solutions(normalized_result.get("solutions"))
    solution_titles = [
        _as_str(item.get("title"))
        for item in solutions
        if isinstance(item, dict) and _as_str(item.get("title"))
    ]

    transcript_lines = [
        f"session_id: {session_id}",
        f"analysis_type: {analysis_type}",
        f"service_name: {service_name}",
        f"trace_id: {trace_id or 'N/A'}",
        f"session_summary: {summary_text}",
        f"input_text: {input_text}",
        f"baseline_problem_type: {overview_problem}",
        f"baseline_severity: {overview_severity}",
        f"baseline_analysis_summary: {overview_description}",
        f"baseline_root_causes: {json.dumps(root_causes[:8], ensure_ascii=False)}",
        f"baseline_solutions: {json.dumps(solution_titles[:8], ensure_ascii=False)}",
    ]
    if dialogue_lines:
        transcript_lines.append("conversation:")
        transcript_lines.extend(dialogue_lines)
    else:
        transcript_lines.append("conversation: []")

    return "\n".join(transcript_lines)


async def _build_llm_kb_draft(
    session: Dict[str, Any],
    messages: List[Any],
    include_followup: bool,
    fallback_draft: Dict[str, Any],
) -> Dict[str, Any]:
    """使用 LLM 对整段会话进行归纳，生成知识草稿结构。"""
    result_container = session.get("result") if isinstance(session, dict) else {}
    raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
    normalized_result = _normalize_analysis_result(raw_result, fallback_description=_as_str(session.get("summary_text")))
    transcript = _build_kb_conversation_transcript(
        session,
        messages,
        include_followup=include_followup,
        normalized_result=normalized_result,
    )

    llm_timeout_seconds = max(5, int(_as_float(os.getenv("AI_KB_DRAFT_LLM_TIMEOUT_SECONDS", 45), 45)))
    llm_service = get_llm_service()
    prompt = (
        "请基于以下完整分析会话生成知识库草稿，输出严格 JSON（不要 markdown、不要额外解释）。\n"
        "JSON schema:\n"
        "{\n"
        '  "problem_type": "string",\n'
        '  "severity": "critical|high|medium|low|unknown",\n'
        '  "summary": "string",\n'
        '  "analysis_summary": "string",\n'
        '  "root_causes": ["string"],\n'
        '  "solutions": [{"title":"string","description":"string","steps":["string"]}],\n'
        '  "manual_remediation_steps": ["string"],\n'
        '  "confidence": 0.0\n'
        "}\n"
        "约束：\n"
        "1) root_causes 3-8 条，短句且可执行；\n"
        "2) solutions 1-6 条，每条要包含 title，steps 可选；\n"
        "3) manual_remediation_steps 0-8 条；\n"
        "4) 若信息不足，用基于现有上下文最合理的推断，不要留空对象。\n\n"
        "会话内容：\n"
        f"{transcript}"
    )

    response_text = await asyncio.wait_for(
        llm_service.chat(
            message=prompt,
            context={
                "analysis_session_id": _as_str(session.get("session_id")),
                "analysis_type": _as_str(session.get("analysis_type"), "log"),
                "service_name": _as_str(session.get("service_name")),
                "include_followup": bool(include_followup),
                "conversation_transcript": transcript,
            },
        ),
        timeout=llm_timeout_seconds,
    )
    parsed = _parse_llm_json_dict(response_text, as_str=_as_str)
    if parsed is None:
        raise ValueError("llm_kb_draft_parse_failed")

    solution_source = (
        parsed.get("solutions")
        if parsed.get("solutions") is not None
        else parsed.get("recommendations")
    )
    llm_draft = {
        "problem_type": _as_str(
            parsed.get("problem_type") or parsed.get("problemType"),
            fallback_draft.get("problem_type"),
        ).lower(),
        "severity": _normalize_kb_draft_severity(
            parsed.get("severity"),
            default=_as_str(fallback_draft.get("severity"), "medium"),
        ),
        "summary": _as_str(parsed.get("summary"), _as_str(fallback_draft.get("summary"))),
        "analysis_summary": _as_str(
            parsed.get("analysis_summary"),
            _as_str(parsed.get("summary"), _as_str(fallback_draft.get("analysis_summary"))),
        ),
        "root_causes": _normalize_string_list(
            parsed.get("root_causes") if parsed.get("root_causes") is not None else parsed.get("rootCauses"),
            max_items=8,
            min_length=2,
        ),
        "solutions": _normalize_solutions(solution_source),
        "manual_remediation_steps": _normalize_string_list(
            parsed.get("manual_remediation_steps")
            if parsed.get("manual_remediation_steps") is not None
            else parsed.get("manualRemediationSteps"),
            max_items=8,
            min_length=4,
        ),
        "log_content": _as_str(fallback_draft.get("log_content")),
        "service_name": _as_str(fallback_draft.get("service_name")),
    }

    if not llm_draft["root_causes"]:
        llm_draft["root_causes"] = _normalize_string_list(fallback_draft.get("root_causes"), max_items=8, min_length=2)
    if not llm_draft["solutions"]:
        llm_draft["solutions"] = _normalize_solutions(fallback_draft.get("solutions"))
    if not llm_draft["manual_remediation_steps"]:
        llm_draft["manual_remediation_steps"] = _normalize_string_list(
            fallback_draft.get("manual_remediation_steps"),
            max_items=8,
            min_length=4,
        )

    confidence = _as_float(parsed.get("confidence"), 0.88)
    return {"draft_case": llm_draft, "confidence": max(0.0, min(confidence, 1.0))}


def _build_case_payload_for_remote(case_obj: Any) -> Dict[str, Any]:
    """构建远端同步 payload。"""
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    return {
        "id": case_obj.id,
        "external_doc_id": _as_str(llm_metadata.get("external_doc_id")),
        "problem_type": case_obj.problem_type,
        "severity": case_obj.severity,
        "summary": case_obj.summary,
        "log_content": case_obj.log_content,
        "service_name": case_obj.service_name,
        "root_causes": case_obj.root_causes or [],
        "solutions": case_obj.solutions or [],
        "resolution": case_obj.resolution,
        "resolved": bool(case_obj.resolved),
        "case_status": _get_case_status(case_obj),
        "manual_remediation_steps": llm_metadata.get("manual_remediation_steps", []),
        "verification_result": _as_str(llm_metadata.get("verification_result")),
        "verification_notes": _as_str(llm_metadata.get("verification_notes")),
        "knowledge_version": int(_as_float(llm_metadata.get("knowledge_version", 1), 1)),
        "updated_at": case_obj.updated_at,
        "context": case_obj.context if isinstance(case_obj.context, dict) else {},
    }


def _extract_overview_summary(result: Dict[str, Any]) -> str:
    overview = result.get("overview") if isinstance(result, dict) else {}
    if isinstance(overview, dict):
        description = _as_str(overview.get("description"))
        if description:
            return description
    return _as_str(result.get("summary"))


def _mask_sensitive_text(text: str) -> str:
    """脱敏文本，避免敏感字段进入会话存储或 LLM 上下文。"""
    value = str(text or "")
    if not value:
        return ""

    masked = value
    masked = re.sub(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]+=*\b", r"\1 ***", masked)
    masked = re.sub(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*([^\s,;]+)",
        lambda m: f"{m.group(1)}=***",
        masked,
    )
    masked = re.sub(
        r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
        r"\1***@\2",
        masked,
    )
    masked = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "***.***.***.***", masked)
    masked = re.sub(r"\bAKIA[0-9A-Z]{12,}\b", "AKIA***", masked)
    masked = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", lambda m: m.group(0)[:4] + "***" + m.group(0)[-2:], masked)
    return masked


def _mask_sensitive_payload(payload: Any) -> Any:
    """递归脱敏结构化对象。"""
    if isinstance(payload, dict):
        return {str(key): _mask_sensitive_payload(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_mask_sensitive_payload(item) for item in payload]
    if isinstance(payload, str):
        return _mask_sensitive_text(payload)
    return payload


def _estimate_token_usage(*parts: Any) -> int:
    """粗略估算 token 数，按字符数/4。"""
    total_chars = 0
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (dict, list)):
            total_chars += len(str(part))
        else:
            total_chars += len(str(part))
    return max(1, total_chars // 4)


async def _persist_analysis_session(
    *,
    analysis_type: str,
    service_name: str,
    input_text: str,
    trace_id: str = "",
    context: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    source: str,
) -> str:
    """把分析请求及结果持久化为 AI 历史会话。"""
    try:
        session_store = get_ai_session_store(storage)
        normalized_result = result or {}
        llm_metadata = normalized_result if isinstance(normalized_result, dict) else {}
        summary_text = _extract_overview_summary(normalized_result)
        safe_context = _mask_sensitive_payload(context or {})
        safe_input_text = _mask_sensitive_text(input_text)
        session = await _run_blocking(
            session_store.create_session,
            analysis_type=analysis_type,
            service_name=service_name,
            input_text=safe_input_text,
            trace_id=trace_id,
            context=safe_context,
            result={
                "summary": summary_text,
                "raw": _mask_sensitive_payload(normalized_result),
            },
            analysis_method=_as_str(llm_metadata.get("analysis_method"), "unknown"),
            llm_model=_as_str(llm_metadata.get("model")),
            llm_provider=_as_str((context or {}).get("llm_provider") or os.getenv("LLM_PROVIDER", "")),
            source=source,
            summary_text=summary_text,
        )
        return session.session_id
    except Exception as e:
        logger.warning(f"Failed to persist AI analysis session: {e}")
        return ""


@router.post("/analyze-log")
async def analyze_log(request: AnalyzeLogRequest) -> Dict[str, Any]:
    """
    分析单条日志（基于规则）

    基于日志内容、级别和服务信息，智能识别问题并提供：
    - 问题概述
    - 根因分析
    - 解决方案建议
    - 影响指标
    - 相似案例
    """
    try:
        analyzer = get_log_analyzer(storage)

        log_data = {
            'id': request.id,
            'timestamp': request.timestamp,
            'entity': request.entity,
            'event': request.event,
            'context': request.context
        }

        result = await _run_blocking(analyzer.analyze_log, log_data)
        normalized = _normalize_analysis_result(result)
        session_id = await _persist_analysis_session(
            analysis_type="log",
            service_name=_as_str(request.entity.get("name") if isinstance(request.entity, dict) else ""),
            input_text=_as_str(
                (request.event.get("raw") if isinstance(request.event, dict) else "")
                or (request.event.get("message") if isinstance(request.event, dict) else "")
            ),
            trace_id=_as_str((request.context or {}).get("trace_id")),
            context=request.context or {},
            result=normalized,
            source="api:/analyze-log",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing log: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-log-llm")
async def analyze_log_llm(request: LLMAnalyzeRequest) -> Dict[str, Any]:
    """
    分析单条日志（使用 LLM 大模型）

    使用 GPT-4 或 Claude 等大模型进行深度分析：
    - 更准确的问题识别
    - 更详细的根因分析
    - 更专业的解决方案
    - 相似案例推荐

    需要配置 LLM_API_KEY（或 provider 对应 key）环境变量
    """
    try:
        safe_context = dict(request.context or {})
        if request.enable_web_search:
            safe_context["enable_web_search"] = True

        prepared_input = request.log_content
        prepared_context = safe_context
        preparation_notice = ""
        request_flow_agent = None
        preparation = None
        if request.enable_agent:
            try:
                request_flow_agent = get_request_flow_agent(storage)
                preparation = await _run_blocking(
                    request_flow_agent.prepare_analysis_input,
                    log_content=request.log_content,
                    service_name=request.service_name,
                    context=safe_context,
                )
                prepared_input = preparation.log_content
                prepared_context = preparation.context
                preparation_notice = preparation.notice
            except Exception as prep_error:
                logger.warning("Request flow agent prepare failed, fallback to raw input: %s", prep_error)
                request_flow_agent = None

        llm_enabled = _is_llm_configured()
        
        if not llm_enabled or not request.use_llm:
            analyzer = get_log_analyzer(storage)
            log_data = {
                'id': 'llm-fallback',
                'timestamp': '',
                'entity': {'name': request.service_name},
                'event': {'level': 'error', 'raw': prepared_input},
                'context': prepared_context
            }
            result = await _run_blocking(analyzer.analyze_log, log_data)
            if request_flow_agent and preparation is not None:
                result = request_flow_agent.augment_result(result, preparation)
            normalized = _normalize_analysis_result(result, analysis_method="rule-based")
            if preparation_notice:
                normalized["agent_notice"] = preparation_notice
            session_id = await _persist_analysis_session(
                analysis_type="log",
                service_name=request.service_name,
                input_text=request.log_content,
                trace_id=_as_str(prepared_context.get("trace_id")),
                context=prepared_context,
                result=normalized,
                source="api:/analyze-log-llm:rule",
            )
            if session_id:
                normalized["session_id"] = session_id
            return normalized

        llm_service = get_llm_service()
        
        result = await llm_service.analyze_log(
            log_content=prepared_input,
            service_name=request.service_name,
            context=prepared_context,
        )
        if request_flow_agent and preparation is not None:
            result = request_flow_agent.augment_result(result, preparation)
        normalized = _normalize_analysis_result(result, analysis_method="llm")
        if preparation_notice:
            normalized["agent_notice"] = preparation_notice
        session_id = await _persist_analysis_session(
            analysis_type="log",
            service_name=request.service_name,
            input_text=request.log_content,
            trace_id=_as_str(prepared_context.get("trace_id")),
            context=prepared_context,
            result=normalized,
            source="api:/analyze-log-llm",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing log with LLM: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-trace")
async def analyze_trace(request: AnalyzeTraceRequest) -> Dict[str, Any]:
    """
    分析整个调用链（基于规则）

    分析 trace_id 对应的完整调用链，识别：
    - 异常服务
    - 慢操作
    - 性能瓶颈
    - 调用链问题
    """
    try:
        trace_id = (request.trace_id or "").strip()
        if not trace_id:
            raise HTTPException(status_code=400, detail="trace_id is required")

        analyzer = get_log_analyzer(storage)
        result = await _run_blocking(analyzer.analyze_trace, trace_id, storage)
        normalized = _normalize_analysis_result(result)
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name="",
            input_text=trace_id,
            trace_id=trace_id,
            context={"trace_id": trace_id},
            result=normalized,
            source="api:/analyze-trace",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing trace: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/analyze-trace-llm")
async def analyze_trace_llm(request: LLMTraceAnalyzeRequest) -> Dict[str, Any]:
    """
    分析调用链（使用 LLM 大模型）

    使用大模型进行深度链路分析
    """
    try:
        llm_enabled = _is_llm_configured()
        
        if not llm_enabled:
            normalized = _normalize_analysis_result(
                {"error": "LLM not configured"},
                analysis_method="none",
                fallback_description="请配置 LLM_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 环境变量以启用 LLM 分析",
            )
            session_id = await _persist_analysis_session(
                analysis_type="trace",
                service_name=request.service_name,
                input_text=request.trace_id,
                trace_id=request.trace_id,
                context={"trace_id": request.trace_id},
                result=normalized,
                source="api:/analyze-trace-llm:none",
            )
            if session_id:
                normalized["session_id"] = session_id
            return normalized

        llm_service = get_llm_service()
        
        result = await llm_service.analyze_trace(
            trace_data=request.trace_id,
            service_name=request.service_name,
        )
        normalized = _normalize_analysis_result(result, analysis_method="llm")
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name=request.service_name,
            input_text=request.trace_id,
            trace_id=request.trace_id,
            context={"trace_id": request.trace_id},
            result=normalized,
            source="api:/analyze-trace-llm",
        )
        if session_id:
            normalized["session_id"] = session_id
        return normalized

    except Exception as e:
        logger.error(f"Error analyzing trace with LLM: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/llm/runtime")
async def get_llm_runtime_status() -> Dict[str, Any]:
    """获取 LLM 运行时状态与预留配置契约。"""
    return _build_llm_runtime_status()


@router.post("/llm/runtime/validate")
async def validate_llm_runtime_config(request: LLMRuntimeConfig) -> Dict[str, Any]:
    """校验本地/远端 LLM 运行时配置结构（预留接口，不会落盘）。"""
    normalized = {
        "provider": _as_str(request.provider, "openai"),
        "model": _as_str(request.model),
        "api_base": _as_str(request.api_base),
        "local_model_path": _as_str(request.local_model_path),
        "extra": request.extra or {},
    }
    if normalized["provider"] not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    return {
        "status": "ok",
        "validated": True,
        "runtime": normalized,
        "note": "当前仅校验参数结构；后续可将该配置接入本地 LLM 动态路由能力。",
    }


@router.post("/llm/runtime/update")
async def update_llm_runtime_config(request: LLMRuntimeUpdateRequest) -> Dict[str, Any]:
    """更新 LLM 运行时配置（当前进程生效，支持 API key 更新）。"""
    normalized = {
        "provider": _as_str(request.provider, "openai"),
        "model": _as_str(request.model),
        "api_base": _as_str(request.api_base),
        "api_key": _as_str(request.api_key),
        "local_model_path": _as_str(request.local_model_path),
        "clear_api_key": bool(request.clear_api_key),
        "persist_to_deployment": bool(request.persist_to_deployment),
        "extra": request.extra or {},
    }

    if normalized["provider"] not in SUPPORTED_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail="unsupported provider")

    _apply_llm_runtime_update(normalized)
    deployment_file = _resolve_llm_deployment_file_path(normalized["extra"])
    persistence_result = {
        "persisted": False,
        "deployment_file": deployment_file,
        "updated_keys": [],
        "added_keys": [],
        "error": "deployment persistence disabled",
    }
    if normalized["persist_to_deployment"]:
        persistence_result = _persist_llm_runtime_to_deployment_file(normalized, deployment_file)

    reset_llm_service()

    note = "配置已更新到当前进程运行时。"
    if normalized["persist_to_deployment"]:
        if persistence_result.get("persisted"):
            note += " 已同步写入部署文件。"
        else:
            note += (
                " 部署文件持久化失败，当前仍仅进程生效；"
                f"原因: {persistence_result.get('error') or 'unknown'}。"
            )
    else:
        note += " 已跳过部署文件持久化。"

    return {
        "status": "ok",
        "updated": True,
        "runtime": {
            "provider": normalized["provider"],
            "model": normalized["model"],
            "api_base": normalized["api_base"],
            "local_model_path": normalized["local_model_path"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "deployment_persistence": persistence_result,
        "runtime_status": _build_llm_runtime_status(),
        "note": note,
    }


@router.get("/kb/runtime")
async def get_kb_runtime_status() -> Dict[str, Any]:
    """获取远端知识库运行时配置与连通状态。"""
    return _build_kb_runtime_status(force_refresh_provider_status=True)


@router.post("/kb/runtime/validate")
async def validate_kb_runtime_config(request: KBRemoteRuntimeConfig) -> Dict[str, Any]:
    """校验远端知识库运行时参数（不落盘）。"""
    normalized = _normalize_kb_runtime_config(
        request,
        supported_kb_remote_providers=SUPPORTED_KB_REMOTE_PROVIDERS,
    )
    if normalized["provider"] != "disabled" and not _as_str(normalized["base_url"]):
        raise HTTPException(status_code=400, detail="base_url is required when provider is enabled")
    if normalized["provider"] == "ragflow" and not _as_str(normalized.get("dataset_id")):
        raise HTTPException(status_code=400, detail="dataset_id is required when provider=ragflow")

    return {
        "status": "ok",
        "validated": True,
        "runtime": {
            "provider": normalized["provider"],
            "base_url": normalized["base_url"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "dataset_id": normalized.get("dataset_id", ""),
            "timeout_seconds": normalized["timeout_seconds"],
            "health_path": normalized["health_path"],
            "search_path": normalized["search_path"],
            "upsert_path": normalized["upsert_path"],
            "outbox_enabled": normalized["outbox_enabled"],
            "outbox_poll_seconds": normalized["outbox_poll_seconds"],
            "outbox_max_attempts": normalized["outbox_max_attempts"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "note": "参数结构校验通过；若 provider=ragflow，需配置 dataset_id，并按原生 datasets/documents API 对接。",
    }


@router.post("/kb/runtime/update")
async def update_kb_runtime_config(request: KBRemoteRuntimeConfig) -> Dict[str, Any]:
    """更新远端知识库运行时配置（支持 RAGFlow 默认配置）。"""
    normalized = _normalize_kb_runtime_config(
        request,
        supported_kb_remote_providers=SUPPORTED_KB_REMOTE_PROVIDERS,
    )
    if normalized["provider"] != "disabled" and not _as_str(normalized["base_url"]):
        raise HTTPException(status_code=400, detail="base_url is required when provider is enabled")
    if normalized["provider"] == "ragflow" and not _as_str(normalized.get("dataset_id")):
        raise HTTPException(status_code=400, detail="dataset_id is required when provider=ragflow")
    if normalized["api_key"] and normalized["clear_api_key"]:
        raise HTTPException(status_code=400, detail="api_key and clear_api_key cannot both be set")

    _apply_kb_runtime_update(normalized)

    deployment_file = _resolve_kb_deployment_file_path(normalized["extra"])
    persistence_result = {
        "persisted": False,
        "deployment_file": deployment_file,
        "updated_keys": [],
        "added_keys": [],
        "error": "deployment persistence disabled",
    }
    if normalized["persist_to_deployment"]:
        persistence_result = _persist_kb_runtime_to_deployment_file(normalized, deployment_file)

    # 远端 KB 配置更新后重建网关，确保 provider/outbox 参数实时生效。
    gateway = reload_knowledge_gateway(storage)
    gateway.start_outbox_worker()

    note = "KB 运行时配置已更新到当前进程。"
    if normalized["persist_to_deployment"]:
        if persistence_result.get("persisted"):
            note += " 已同步写入部署文件。"
        else:
            note += (
                " 部署文件持久化失败，当前仍仅进程生效；"
                f"原因: {persistence_result.get('error') or 'unknown'}。"
            )
    else:
        note += " 已跳过部署文件持久化。"

    return {
        "status": "ok",
        "updated": True,
        "runtime": {
            "provider": normalized["provider"],
            "base_url": normalized["base_url"],
            "api_key_updated": bool(normalized["api_key"]),
            "clear_api_key": normalized["clear_api_key"],
            "dataset_id": normalized.get("dataset_id", ""),
            "timeout_seconds": normalized["timeout_seconds"],
            "health_path": normalized["health_path"],
            "search_path": normalized["search_path"],
            "upsert_path": normalized["upsert_path"],
            "outbox_enabled": normalized["outbox_enabled"],
            "outbox_poll_seconds": normalized["outbox_poll_seconds"],
            "outbox_max_attempts": normalized["outbox_max_attempts"],
            "persist_to_deployment": normalized["persist_to_deployment"],
            "extra": normalized["extra"],
        },
        "deployment_persistence": persistence_result,
        "runtime_status": _build_kb_runtime_status(force_refresh_provider_status=True),
        "note": note,
    }


class SimilarCasesRequest(BaseModel):
    """相似案例查询请求"""
    log_content: str
    service_name: str = ""
    problem_type: str = ""
    context: Dict[str, Any] = {}
    limit: int = 5
    include_draft: bool = False


class SaveCaseRequest(BaseModel):
    """保存案例请求"""
    problem_type: str
    severity: str
    summary: str
    log_content: str
    service_name: str = ""
    root_causes: List[str] = []
    solutions: List[Dict[str, Any]] = []
    context: Dict[str, Any] = {}
    tags: List[str] = []
    llm_provider: str = ""
    llm_model: str = ""
    llm_metadata: Dict[str, Any] = {}
    source: str = "manual"
    save_mode: str = "local_only"
    remote_enabled: bool = False


class ResolveCaseRequest(BaseModel):
    """标记案例已解决请求"""
    resolution: str = ""


class KBRuntimeOptionsRequest(BaseModel):
    """知识库运行时策略请求。"""
    remote_enabled: bool = False
    retrieval_mode: str = "local"
    save_mode: str = "local_only"


class KBSearchRequest(BaseModel):
    """统一知识检索请求。"""
    query: str
    service_name: str = ""
    problem_type: str = ""
    top_k: int = 5
    retrieval_mode: str = "local"
    include_draft: bool = False


class KBFromAnalysisSessionRequest(BaseModel):
    """从分析会话生成知识草稿请求。"""
    analysis_session_id: str
    include_followup: bool = True
    history: List[Dict[str, Any]] = []
    use_llm: bool = True
    save_mode: str = "local_only"
    remote_enabled: bool = False


class ManualRemediationRequest(BaseModel):
    """人工修复步骤更新请求。"""
    manual_remediation_steps: List[str]
    verification_result: str
    verification_notes: str
    final_resolution: str = ""
    save_mode: str = "local_only"
    remote_enabled: bool = False


class UpdateCaseContentRequest(BaseModel):
    """更新知识库内容请求。"""
    problem_type: Optional[str] = None
    severity: Optional[str] = None
    summary: Optional[str] = None
    service_name: Optional[str] = None
    root_causes: Optional[List[str]] = None
    solutions: Optional[List[Dict[str, Any]]] = None
    solutions_text: Optional[str] = None
    analysis_summary: Optional[str] = None
    resolution: Optional[str] = None
    tags: Optional[List[str]] = None
    save_mode: str = "local_only"
    remote_enabled: bool = False


class KBSolutionOptimizeRequest(BaseModel):
    """知识库解决建议文本优化请求。"""
    content: str
    summary: str = ""
    service_name: str = ""
    problem_type: str = ""
    severity: str = "medium"
    use_llm: bool = True


class FollowUpMessage(BaseModel):
    """追问消息"""
    role: str
    content: str
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    metadata: Dict[str, Any] = {}


class FollowUpRequest(BaseModel):
    """追问请求"""
    question: str
    analysis_session_id: str = ""
    conversation_id: str = ""
    use_llm: bool = True
    show_thought: bool = False
    auto_exec_readonly: bool = True
    analysis_context: Dict[str, Any] = {}
    history: List[FollowUpMessage] = []
    reset: bool = False


class HistorySessionUpdateRequest(BaseModel):
    """AI 历史会话更新请求（重命名/Pin/归档）。"""
    title: Optional[str] = None
    is_pinned: Optional[bool] = None
    is_archived: Optional[bool] = None
    status: Optional[str] = None


class FollowUpActionRequest(BaseModel):
    """将回答转换为可执行动作。"""
    action_type: str
    title: str = ""
    extra: Dict[str, Any] = {}


class FollowUpCommandExecuteRequest(BaseModel):
    """执行 AI 追问回答中提取出的命令。"""
    command: str = ""
    command_spec: Dict[str, Any] = Field(default_factory=dict)
    purpose: str = ""
    confirmed: bool = False
    elevated: bool = False
    confirmation_ticket: str = ""
    client_deadline_ms: int = 0
    timeout_seconds: int = _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT


@router.post("/similar-cases")
async def find_similar_cases(request: SimilarCasesRequest) -> Dict[str, Any]:
    """
    查找相似案例

    基于日志内容、服务名称和问题类型，检索历史相似案例
    """
    try:
        from ai.similar_cases import get_recommender, get_case_store

        recommender = get_recommender(storage)
        case_store = get_case_store(storage)

        query_kwargs: Dict[str, Any] = {
            "log_content": request.log_content,
            "service_name": request.service_name,
            "problem_type": request.problem_type,
            "context": request.context or {},
            "limit": request.limit,
            "min_similarity": 0.2,
        }
        if request.include_draft:
            query_kwargs["include_draft"] = True

        results = recommender.find_similar_cases(**query_kwargs)

        items: List[Dict[str, Any]] = []
        for r in results:
            content_history_recent = _case_store_list_change_history(
                case_store,
                r.case.id,
                warn=logger.warning,
                limit=3,
                event_type="content_update",
            )
            content_history_count = _case_store_count_change_history(
                case_store,
                r.case.id,
                warn=logger.warning,
                event_type="content_update",
            )
            if content_history_count <= 0:
                llm_metadata = getattr(r.case, "llm_metadata", {}) if hasattr(r.case, "llm_metadata") else {}
                if not isinstance(llm_metadata, dict):
                    llm_metadata = {}
                content_history_count = len(_as_list(llm_metadata.get("content_update_history")))
            items.append(
                {
                    "id": r.case.id,
                    "problem_type": r.case.problem_type,
                    "severity": r.case.severity,
                    "summary": r.case.summary,
                    "service_name": r.case.service_name,
                    "root_causes": r.case.root_causes,
                    "solutions": r.case.solutions,
                    "resolved": r.case.resolved,
                    "resolution": r.case.resolution,
                    "tags": r.case.tags,
                    "case_status": _get_case_status(r.case),
                    "similarity_score": r.similarity_score,
                    "matched_features": r.matched_features,
                    "relevance_reason": r.relevance_reason,
                    "content_update_history_count": content_history_count,
                    "content_update_history_recent": content_history_recent,
                }
            )
        return {
            "cases": items,
            "total": len(items),
            "query": {
                "service_name": request.service_name,
                "problem_type": request.problem_type,
            }
        }

    except Exception as e:
        logger.error(f"Error finding similar cases: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cases")
async def save_case(request: SaveCaseRequest) -> Dict[str, Any]:
    """
    保存新案例到案例库

    将分析结果保存为历史案例，供后续相似案例检索使用
    """
    try:
        from ai.similar_cases import get_case_store, Case, FeatureExtractor
        import uuid

        case_store = get_case_store(storage)
        save_mode = request.save_mode if request.save_mode in {"local_only", "local_and_remote"} else "local_only"
        remote_enabled = bool(request.remote_enabled)
        gateway = None
        effective_save_mode = "local_only"
        if remote_enabled or save_mode == "local_and_remote":
            gateway = get_knowledge_gateway(storage)
            runtime_options = gateway.resolve_runtime_options(
                remote_enabled=remote_enabled,
                retrieval_mode="local",
                save_mode=save_mode,
            )
            effective_save_mode = _as_str(runtime_options.get("effective_save_mode"), "local_only")

        llm_metadata = request.llm_metadata or {}
        if not isinstance(llm_metadata, dict):
            llm_metadata = {}
        llm_metadata = dict(llm_metadata)
        llm_metadata.setdefault("case_status", "archived")
        llm_metadata.setdefault("knowledge_version", 1)
        llm_metadata.setdefault("verification_result", "")
        llm_metadata.setdefault("verification_notes", "")
        llm_metadata.setdefault("manual_remediation_steps", [])
        llm_metadata.setdefault("remediation_history", [])
        llm_metadata.setdefault("content_update_history", [])
        llm_metadata.setdefault("sync_status", "not_requested")
        llm_metadata.setdefault("external_doc_id", "")
        llm_metadata.setdefault("sync_error", "")
        llm_metadata.setdefault("sync_error_code", "")

        case = Case(
            id=f"case-{uuid.uuid4().hex[:8]}",
            problem_type=request.problem_type,
            severity=request.severity,
            summary=request.summary,
            log_content=request.log_content,
            service_name=request.service_name,
            root_causes=request.root_causes,
            solutions=request.solutions,
            context=request.context or {},
            tags=request.tags,
            created_at=datetime.now().isoformat(),
            llm_provider=request.llm_provider,
            llm_model=request.llm_model,
            llm_metadata=llm_metadata,
            source=request.source or "manual",
        )

        case.similarity_features = FeatureExtractor.extract_features(
            case.log_content,
            case.service_name,
            context=request.context or {},
        )

        remote_result: Dict[str, Any] = {
            "sync_status": "not_requested",
            "external_doc_id": "",
            "sync_error": "",
            "sync_error_code": "",
            "outbox_id": "",
        }
        if effective_save_mode != "local_only":
            if gateway is None:
                gateway = get_knowledge_gateway(storage)
            remote_result = gateway.upsert_remote_with_outbox(
                _build_case_payload_for_remote(case),
                save_mode=effective_save_mode,
            )
        llm_meta_copy = case.llm_metadata if isinstance(case.llm_metadata, dict) else {}
        llm_meta_copy = dict(llm_meta_copy)
        llm_meta_copy["sync_status"] = _as_str(remote_result.get("sync_status"), "not_requested")
        llm_meta_copy["external_doc_id"] = _as_str(remote_result.get("external_doc_id"))
        llm_meta_copy["sync_error"] = _as_str(remote_result.get("sync_error"))
        llm_meta_copy["sync_error_code"] = _as_str(remote_result.get("sync_error_code"))
        case.llm_metadata = llm_meta_copy

        case_store.add_case(case)

        return {
            "id": case.id,
            "message": "Case saved successfully",
            "created_at": case.created_at,
            "effective_save_mode": effective_save_mode,
            "sync_status": llm_meta_copy.get("sync_status"),
            "external_doc_id": llm_meta_copy.get("external_doc_id"),
            "sync_error": llm_meta_copy.get("sync_error"),
            "sync_error_code": llm_meta_copy.get("sync_error_code"),
            "outbox_id": _as_str(remote_result.get("outbox_id")),
        }

    except Exception as e:
        logger.error(f"Error saving case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/cases")
async def list_cases(
    problem_type: Optional[str] = None,
    service_name: Optional[str] = None,
    limit: int = 20
) -> Dict[str, Any]:
    """
    列出案例库中的案例
    """
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)

        if problem_type:
            cases = case_store.get_cases_by_type(problem_type)
        elif service_name:
            cases = case_store.get_cases_by_service(service_name)
        else:
            cases = case_store.get_all_cases()

        cases = cases[:limit]
        case_items = _build_case_list_items(
            cases,
            case_store=case_store,
            case_store_count_change_history=lambda store, case_id, event_type: _case_store_count_change_history(
                store,
                case_id,
                warn=logger.warning,
                event_type=event_type,
            ),
            as_list=_as_list,
            as_str=_as_str,
            as_float=_as_float,
            get_case_status=_get_case_status,
        )
        return {
            "cases": case_items,
            "total": len(case_items),
        }

    except Exception as e:
        logger.error(f"Error listing cases: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def list_ai_history(
    limit: int = 20,
    offset: int = 0,
    analysis_type: Optional[str] = None,
    service_name: Optional[str] = None,
    q: Optional[str] = Query(default=None, description="按会话标题/输入/追问内容搜索"),
    include_archived: bool = False,
    pinned_first: bool = True,
    sort_by: str = Query(
        default="updated_at",
        description="排序字段: updated_at|created_at|title|service_name|analysis_type",
    ),
    sort_order: str = Query(default="desc", description="排序方向: asc|desc"),
) -> Dict[str, Any]:
    """列出 AI 分析会话历史。"""
    try:
        session_store = get_ai_session_store(storage)
        normalized = _normalize_history_list_request(
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            allowed_sort_fields=ALLOWED_SESSION_SORT_FIELDS,
            allowed_sort_orders=ALLOWED_SESSION_SORT_ORDERS,
        )
        safe_limit = int(normalized["limit"])
        safe_offset = int(normalized["offset"])
        safe_sort_by = str(normalized["sort_by"])
        safe_sort_order = str(normalized["sort_order"])
        sessions, total_all = await _run_blocking(
            session_store.list_sessions_with_total,
            limit=safe_limit,
            offset=safe_offset,
            analysis_type=analysis_type or "",
            service_name=service_name or "",
            include_archived=include_archived,
            search_query=q or "",
            pinned_first=pinned_first,
            sort_by=safe_sort_by,
            sort_order=safe_sort_order,
        )
        session_ids = [session.session_id for session in sessions]
        message_counts = await _run_blocking(session_store.get_message_counts, session_ids)
        items = _build_history_list_items(
            sessions,
            message_counts=message_counts if isinstance(message_counts, dict) else {},
            as_str=_as_str,
        )
        return _build_history_list_response(
            items=items,
            total_all=total_all,
            safe_limit=safe_limit,
            safe_offset=safe_offset,
            safe_sort_by=safe_sort_by,
            safe_sort_order=safe_sort_order,
            pinned_first=pinned_first,
        )
    except Exception as e:
        logger.error(f"Error listing AI history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/history/{session_id}")
async def update_ai_history_session(session_id: str, request: HistorySessionUpdateRequest) -> Dict[str, Any]:
    """更新 AI 历史会话元信息（重命名、Pin、归档、状态）。"""
    try:
        session_store = get_ai_session_store(storage)
        existing = await _run_blocking(session_store.get_session, session_id)
        if not existing:
            raise HTTPException(status_code=404, detail="session not found")

        changes = _collect_history_session_update_changes(
            request,
            existing_status=_as_str(existing.status),
            as_str=_as_str,
        )

        if not changes:
            return _build_history_session_update_noop(existing)

        updated = await _run_blocking(session_store.update_session, session_id, **changes)
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")

        return _build_history_session_update_response(updated)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating AI history session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/history/{session_id}")
async def delete_ai_history_session(session_id: str) -> Dict[str, Any]:
    """删除 AI 历史会话（软删除）。"""
    try:
        session_store = get_ai_session_store(storage)
        deleted = await _run_blocking(session_store.delete_session, session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "status": "ok",
            "session_id": session_id,
            "message": "session deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting AI history session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/history/{session_id}/messages/{message_id}")
async def delete_ai_history_message(session_id: str, message_id: str) -> Dict[str, Any]:
    """删除会话中的单条消息（逻辑删除）。"""
    try:
        session_store = get_ai_session_store(storage)
        deleted = await _run_blocking(session_store.delete_message, session_id, message_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="message not found")
        remaining_count = await _run_blocking(session_store.get_message_count, session_id)
        return {
            "status": "ok",
            "session_id": session_id,
            "message_id": message_id,
            "remaining_message_count": int(remaining_count),
            "message": "history message deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting AI history message: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history/{session_id}")
async def get_ai_history_detail(session_id: str) -> Dict[str, Any]:
    """获取 AI 分析会话详情（请求、分析结果、追问消息）。"""
    try:
        session_store = get_ai_session_store(storage)
        payload = await _run_blocking(session_store.get_session_with_messages, session_id)
        if not payload:
            raise HTTPException(status_code=404, detail="session not found")

        return _build_ai_history_detail_response(
            payload,
            as_str=_as_str,
            build_context_pills=lambda analysis_ctx, sid: _build_context_pills(
                analysis_ctx,
                analysis_session_id=sid,
                extract_overview_summary=_extract_overview_summary,
                mask_sensitive_text=_mask_sensitive_text,
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting AI history detail: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/history/{session_id}/messages/{message_id}/actions")
async def create_followup_action(
    session_id: str,
    message_id: str,
    request: FollowUpActionRequest,
) -> Dict[str, Any]:
    """将某条回答一键转换为工单/Runbook/告警抑制建议。"""
    try:
        session_store = get_ai_session_store(storage)
        session, message_content = await _load_followup_action_context(
            run_blocking=_run_blocking,
            session_store=session_store,
            session_id=session_id,
            message_id=message_id,
            as_str=_as_str,
        )

        draft = _build_followup_action_draft(
            action_type=request.action_type,
            message_content=message_content,
            session=session,
            preferred_title=request.title,
            extra=request.extra or {},
            as_str=_as_str,
            utc_now_iso=_utc_now_iso,
            mask_sensitive_text=_mask_sensitive_text,
        )
        action_id, action_payload = _build_followup_action_payload(
            message_id=message_id,
            draft=draft,
            utc_now_iso=_utc_now_iso,
        )
        session_context = _merge_followup_action_into_context(session, action_payload)
        await _run_blocking(session_store.update_session, session_id, context=session_context)

        return {
            "status": "ok",
            "session_id": session_id,
            "message_id": message_id,
            "action_id": action_id,
            "action": draft,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating follow-up action: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


def _resolve_followup_command_purpose(
    *,
    raw_command: str,
    message_metadata: Dict[str, Any],
    request_purpose: str,
) -> str:
    explicit_purpose = _as_str(request_purpose).strip()
    if explicit_purpose:
        return explicit_purpose[:220]

    match_key = _normalize_followup_command_match_key(raw_command)
    actions = message_metadata.get("actions") if isinstance(message_metadata.get("actions"), list) else []
    for item in actions:
        payload = item if isinstance(item, dict) else {}
        action_command = _normalize_followup_command_line(payload.get("command"))
        if not action_command:
            continue
        action_key = _normalize_followup_command_match_key(action_command)
        if not action_key or action_key != match_key:
            continue
        action_purpose = (
            _as_str(payload.get("purpose")).strip()
            or _as_str(payload.get("title")).strip()
            or _as_str(payload.get("expected_outcome")).strip()
        )
        if action_purpose:
            return action_purpose[:220]

    return f"执行追问建议命令：{raw_command[:160]}"


def _followup_require_spec_for_repair_enabled() -> bool:
    """高风险写命令是否要求 command_spec。默认关闭，按环境变量开启。"""
    return _is_truthy_env("AI_FOLLOWUP_COMMAND_REQUIRE_SPEC_FOR_REPAIR", False)


def _runtime_require_structured_actions_enabled() -> bool:
    """运行态命令执行是否强制要求结构化 ActionSpec。默认开启，可按环境变量降级。"""
    return _is_truthy_env("AI_RUNTIME_REQUIRE_STRUCTURED_ACTIONS", True)


def _build_missing_or_invalid_command_spec_response(
    *,
    session_id: str,
    message_id: str,
    command: str,
    purpose: str,
    detail: str,
    command_spec: Optional[Dict[str, Any]] = None,
    reason: str = "missing_or_invalid_command_spec",
    status: str = "blocked",
) -> Dict[str, Any]:
    safe_status = _as_str(status).strip().lower()
    if safe_status not in {"blocked", "waiting_user_input"}:
        safe_status = "blocked"
    recovery_payload = build_command_spec_self_repair_payload(
        reason=reason,
        detail=detail,
        command_spec=command_spec,
        raw_command=command,
    )
    return {
        "status": safe_status,
        "session_id": session_id,
        "message_id": message_id,
        "command": _normalize_followup_command_line(command),
        "purpose": _as_str(purpose),
        "command_type": "unknown",
        "risk_level": "high",
        "requires_confirmation": False,
        "requires_write_permission": False,
        "requires_elevation": False,
        "message": _as_str(detail).strip() or "缺少或无效的结构化命令定义。",
        "error": {
            "code": "missing_or_invalid_command_spec",
            "message": _as_str(detail).strip() or "missing or invalid command_spec",
            "recovery": recovery_payload,
        },
        "recovery": recovery_payload,
    }


def _build_followup_confirmation_message(*, purpose: str, command: str) -> str:
    return (
        f"执行目的：{_as_str(purpose)}\n"
        f"命令：{_as_str(command)}\n"
        "请确认命令语义后继续执行。"
    )


def _map_followup_exec_response(
    *,
    session_id: str,
    message_id: str,
    command: str,
    purpose: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    safe_payload = payload if isinstance(payload, dict) else {}
    status = _as_str(safe_payload.get("status")).strip().lower() or "failed"
    if status == "completed":
        status = "executed"
    if status not in {
        "ok",
        "executed",
        "failed",
        "cancelled",
        "permission_required",
        "confirmation_required",
        "elevation_required",
    }:
        status = "failed"
    safe_command = _normalize_followup_command_line(_as_str(safe_payload.get("command"), command)) or command
    requires_write_permission = bool(safe_payload.get("requires_write_permission"))
    requires_elevation = bool(safe_payload.get("requires_elevation")) or status == "elevation_required"
    requires_confirmation = bool(safe_payload.get("requires_confirmation")) or status in {
        "confirmation_required",
        "elevation_required",
    }
    response: Dict[str, Any] = {
        "status": status,
        "session_id": session_id,
        "message_id": message_id,
        "command": safe_command,
        "purpose": _as_str(purpose),
        "command_type": _as_str(safe_payload.get("command_type"), "unknown"),
        "risk_level": _as_str(safe_payload.get("risk_level"), "high"),
        "requires_confirmation": requires_confirmation,
        "requires_write_permission": requires_write_permission,
        "requires_elevation": requires_elevation,
        "confirmation_message": _build_followup_confirmation_message(purpose=purpose, command=safe_command),
        "message": _as_str(safe_payload.get("message")),
    }
    confirmation_ticket = _as_str(safe_payload.get("confirmation_ticket"))
    if confirmation_ticket:
        response["confirmation_ticket"] = confirmation_ticket
    ticket_expires_at = safe_payload.get("ticket_expires_at")
    if ticket_expires_at is not None:
        response["ticket_expires_at"] = ticket_expires_at

    command_run_id = _as_str(safe_payload.get("command_run_id") or safe_payload.get("run_id"))
    if command_run_id:
        response["command_run_id"] = command_run_id

    if status in {"executed", "failed", "cancelled"}:
        response.update(
            {
                "exit_code": int(safe_payload.get("exit_code") or 0),
                "duration_ms": int(safe_payload.get("duration_ms") or 0),
                "stdout": _as_str(safe_payload.get("stdout")),
                "stderr": _as_str(safe_payload.get("stderr")),
                "output_truncated": bool(safe_payload.get("output_truncated")),
                "timed_out": bool(safe_payload.get("timed_out")),
            }
        )
    return response


@router.post("/history/{session_id}/messages/{message_id}/commands/execute")
async def execute_followup_command(
    session_id: str,
    message_id: str,
    request: FollowUpCommandExecuteRequest,
) -> Dict[str, Any]:
    """执行 AI 回答中生成的查询/修复命令，并返回执行结果。"""
    try:
        if not _is_truthy_env("AI_FOLLOWUP_COMMAND_EXEC_ENABLED", True):
            return _build_followup_exec_disabled_response(session_id, message_id, request.command)

        session_store = get_ai_session_store(storage)
        message_content, message_metadata, _session_context = await _load_followup_command_message_context(
            run_blocking=_run_blocking,
            session_store=session_store,
            session_id=session_id,
            message_id=message_id,
            as_str=_as_str,
        )
        structured_required = True
        safe_command_spec = normalize_followup_command_spec(request.command_spec)
        if safe_command_spec:
            compile_result = compile_followup_command_spec(safe_command_spec, run_sql_preflight=True)
            if not bool(compile_result.get("ok")):
                compile_reason = _as_str(compile_result.get("reason"), "compile failed")
                compile_detail = _as_str(compile_result.get("detail")).strip()
                detail_message = (
                    f"invalid command_spec: {compile_reason}"
                    if not compile_detail
                    else f"invalid command_spec: {compile_reason}: {compile_detail}"
                )
                if structured_required:
                    return _build_missing_or_invalid_command_spec_response(
                        session_id=session_id,
                        message_id=message_id,
                        command=request.command,
                        purpose=request.purpose,
                        detail=detail_message,
                        command_spec=safe_command_spec,
                        reason=compile_reason,
                        status="blocked",
                    )
                raise HTTPException(status_code=400, detail=detail_message)
            safe_command_spec = (
                compile_result.get("command_spec")
                if isinstance(compile_result.get("command_spec"), dict)
                else safe_command_spec
            )
            raw_command = _normalize_followup_command_line(compile_result.get("command"))
        else:
            if structured_required:
                return _build_missing_or_invalid_command_spec_response(
                    session_id=session_id,
                    message_id=message_id,
                    command=request.command,
                    purpose=request.purpose,
                    detail="missing_or_invalid_command_spec: command_spec is required",
                    command_spec=safe_command_spec,
                    reason="missing_or_invalid_command_spec",
                    status="blocked",
                )
            raw_command = _normalize_followup_command_line(request.command)
        _validate_requested_followup_command(raw_command)
        _assert_followup_command_is_suggested(raw_command, message_content, message_metadata)
        purpose = _resolve_followup_command_purpose(
            raw_command=raw_command,
            message_metadata=message_metadata,
            request_purpose=request.purpose,
        )
        safe_action_id = f"manual-{message_id}"[:64]
        safe_timeout = max(3, min(180, int(request.timeout_seconds or _FOLLOWUP_COMMAND_DEFAULT_TIMEOUT)))
        client_deadline_ms = int(getattr(request, "client_deadline_ms", 0) or 0)
        if client_deadline_ms > 0:
            remaining_ms = max(0, client_deadline_ms - int(time.time() * 1000))
            remaining_seconds = max(1, (remaining_ms + 999) // 1000)
            safe_timeout = max(3, min(safe_timeout, int(remaining_seconds)))

        if not bool(request.confirmed):
            precheck = await precheck_controlled_command(
                session_id=session_id,
                message_id=message_id,
                action_id=safe_action_id,
                command=raw_command,
                purpose=purpose,
                timeout_seconds=min(20, safe_timeout),
            )
            precheck_status = _as_str(precheck.get("status")).lower()
            precheck_command_type = _as_str(precheck.get("command_type")).lower()
            if (
                _followup_require_spec_for_repair_enabled()
                and not safe_command_spec
                and precheck_command_type == "repair"
            ):
                return {
                    "status": "permission_required",
                    "session_id": session_id,
                    "message_id": message_id,
                    "command": _as_str(precheck.get("command"), raw_command),
                    "purpose": purpose,
                    "command_type": precheck_command_type,
                    "risk_level": _as_str(precheck.get("risk_level"), "high"),
                    "requires_confirmation": False,
                    "requires_write_permission": True,
                    "requires_elevation": True,
                    "message": "高风险写命令需提供 command_spec（结构化命令）后才可审批执行。",
                }
            if precheck_status == "ok":
                return {
                    "status": "confirmation_required",
                    "session_id": session_id,
                    "message_id": message_id,
                    "command": _as_str(precheck.get("command"), raw_command),
                    "command_spec": safe_command_spec,
                    "purpose": purpose,
                    "command_type": _as_str(precheck.get("command_type"), "unknown"),
                    "risk_level": _as_str(precheck.get("risk_level"), "high"),
                    "requires_confirmation": True,
                    "requires_write_permission": bool(precheck.get("requires_write_permission")),
                    "requires_elevation": bool(precheck.get("requires_elevation")),
                    "confirmation_message": _build_followup_confirmation_message(
                        purpose=purpose,
                        command=_as_str(precheck.get("command"), raw_command),
                    ),
                    "message": "请先确认命令语义后执行。",
                }
            return _map_followup_exec_response(
                session_id=session_id,
                message_id=message_id,
                command=raw_command,
                purpose=purpose,
                payload=precheck,
            )

        if _followup_require_spec_for_repair_enabled() and not safe_command_spec:
            precheck = await precheck_controlled_command(
                session_id=session_id,
                message_id=message_id,
                action_id=safe_action_id,
                command=raw_command,
                purpose=purpose,
                timeout_seconds=min(20, safe_timeout),
            )
            if _as_str(precheck.get("command_type")).lower() == "repair":
                return {
                    "status": "permission_required",
                    "session_id": session_id,
                    "message_id": message_id,
                    "command": _as_str(precheck.get("command"), raw_command),
                    "purpose": purpose,
                    "command_type": "repair",
                    "risk_level": _as_str(precheck.get("risk_level"), "high"),
                    "requires_confirmation": False,
                    "requires_write_permission": True,
                    "requires_elevation": True,
                    "message": "高风险写命令需提供 command_spec（结构化命令）后才可审批执行。",
                }

        execution_result = await execute_controlled_command(
            session_id=session_id,
            message_id=message_id,
            action_id=safe_action_id,
            command=raw_command,
            purpose=purpose,
            confirmed=bool(request.confirmed),
            elevated=bool(request.elevated),
            confirmation_ticket=_as_str(request.confirmation_ticket),
            timeout_seconds=safe_timeout,
        )
        mapped = _map_followup_exec_response(
            session_id=session_id,
            message_id=message_id,
            command=raw_command,
            purpose=purpose,
            payload=execution_result,
        )
        if safe_command_spec:
            mapped["command_spec"] = safe_command_spec
        return mapped
    except ExecServiceClientError as exc:
        logger.error("Controlled exec gateway unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="受控执行网关不可用，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing follow-up command: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/cases/{case_id}")
async def get_case_detail(case_id: str) -> Dict[str, Any]:
    """获取案例详情（含可回放到 AI 分析页的分析结果结构）。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        case_obj = case_store.get_case(case_id)
        if not case_obj:
            raise HTTPException(status_code=404, detail="Case not found")
        content_history, content_history_count = _resolve_case_detail_content_history(
            case_store=case_store,
            case_obj=case_obj,
            case_store_list_change_history=lambda store, **kwargs: _case_store_list_change_history(
                store,
                warn=logger.warning,
                **kwargs,
            ),
            case_store_count_change_history=lambda store, **kwargs: _case_store_count_change_history(
                store,
                warn=logger.warning,
                **kwargs,
            ),
            as_list=_as_list,
        )
        return _build_case_detail_payload(
            case_obj,
            content_history=content_history,
            content_history_count=content_history_count,
            get_case_status=_get_case_status,
            as_list=_as_list,
            as_str=_as_str,
            as_float=_as_float,
            build_case_analysis_result=_build_case_analysis_result,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting case detail: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/cases/{case_id}")
async def update_case_content(case_id: str, request: UpdateCaseContentRequest) -> Dict[str, Any]:
    """更新知识库内容（摘要、根因、方案等），并支持远端同步策略。"""
    try:
        from ai.similar_cases import get_case_store, Case, FeatureExtractor

        case_store = get_case_store(storage)
        existing = case_store.get_case(case_id)
        if not existing:
            raise HTTPException(status_code=404, detail={"code": "KBR-005", "message": "case not found"})

        requested_fields = _require_editable_fields_for_case_content_update(request)
        updated = Case(**existing.to_dict())
        _apply_case_content_request_fields(
            updated,
            request,
            as_str=_as_str,
            normalize_kb_draft_severity=_normalize_kb_draft_severity,
            truncate_text=_truncate_text,
            normalize_string_list=_normalize_string_list,
            normalize_solutions_from_text=_normalize_solutions_from_text,
            normalize_solutions=_normalize_solutions,
        )
        _validate_case_content_required_fields(updated)

        llm_metadata, knowledge_version, previous_analysis_summary = _prepare_case_content_update_metadata(
            existing,
            updated,
            request,
            as_str=_as_str,
            as_float=_as_float,
            truncate_text=_truncate_text,
            get_case_status=_get_case_status,
            utc_now_iso=_utc_now_iso,
        )
        updated.similarity_features = FeatureExtractor.extract_features(
            updated.log_content,
            updated.service_name,
            context=updated.context or {},
        )
        effective_save_mode, remote_result = _sync_case_update_with_remote(
            updated,
            request,
            gateway=get_knowledge_gateway(storage),
            as_str=_as_str,
            build_case_payload_for_remote=_build_case_payload_for_remote,
        )
        _apply_remote_sync_result_to_case_metadata(
            llm_metadata,
            remote_result,
            as_str=_as_str,
        )
        updated.llm_metadata = llm_metadata

        outcome = _build_case_content_update_outcome(
            existing_case=existing,
            updated_case=updated,
            previous_analysis_summary=previous_analysis_summary,
            current_analysis_summary=_as_str(llm_metadata.get("analysis_summary")),
            requested_fields=requested_fields,
            knowledge_version=knowledge_version,
            effective_save_mode=effective_save_mode,
            sync_status=_as_str(llm_metadata.get("sync_status")),
            sync_error_code=_as_str(llm_metadata.get("sync_error_code")),
            build_case_content_change_summary=lambda **kwargs: _build_case_content_change_summary(
                **kwargs,
                as_str=_as_str,
                as_list=_as_list,
                truncate_text=_truncate_text,
                normalize_solutions=_normalize_solutions,
            ),
            as_list=_as_list,
            as_str=_as_str,
        )
        history_entry = outcome.get("history_entry") if isinstance(outcome.get("history_entry"), dict) else {}

        case_store.update_case(updated)
        persisted_history = _case_store_append_change_history(
            case_store,
            updated.id,
            history_entry,
            warn=logger.warning,
        )
        content_update_history_count = _case_store_count_change_history(
            case_store,
            updated.id,
            warn=logger.warning,
            event_type="content_update",
        )
        if content_update_history_count <= 0:
            content_update_history_count = len(_as_list((updated.llm_metadata or {}).get("content_update_history")))

        return _build_case_content_update_response(
            updated_case=updated,
            knowledge_version=knowledge_version,
            effective_save_mode=effective_save_mode,
            llm_metadata=llm_metadata,
            remote_result=remote_result,
            outcome=outcome,
            requested_fields=requested_fields,
            persisted_history=persisted_history,
            content_update_history_count=content_update_history_count,
            as_list=_as_list,
            as_str=_as_str,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating case content: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.delete("/cases/{case_id}")
async def delete_case(case_id: str) -> Dict[str, Any]:
    """删除案例（ClickHouse 模式为软删除）。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        deleted = case_store.delete_case(case_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Case not found")
        return {
            "status": "ok",
            "id": case_id,
            "message": "Case deleted",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/cases/{case_id}/resolve")
async def resolve_case(case_id: str, request: ResolveCaseRequest) -> Dict[str, Any]:
    """标记案例为已解决。"""
    try:
        from ai.similar_cases import get_case_store

        case_store = get_case_store(storage)
        updated = case_store.mark_case_resolved(case_id, request.resolution)
        if not updated:
            raise HTTPException(status_code=404, detail="Case not found")
        return {
            "status": "ok",
            "id": updated.id,
            "resolved": updated.resolved,
            "resolution": updated.resolution,
            "resolved_at": updated.resolved_at,
            "message": "Case marked as resolved",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving case: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/kb/providers/status")
async def get_kb_providers_status() -> Dict[str, Any]:
    """获取知识库 provider 运行状态。"""
    try:
        gateway = get_knowledge_gateway(storage)
        return gateway.get_provider_status()
    except Exception as e:
        logger.error(f"Error getting KB providers status: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.get("/kb/outbox/status")
async def get_kb_outbox_status() -> Dict[str, Any]:
    """获取远端同步 Outbox 状态。"""
    try:
        gateway = get_knowledge_gateway(storage)
        return gateway.get_outbox_status()
    except Exception as e:
        logger.error(f"Error getting KB outbox status: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/runtime/options")
async def kb_runtime_options(request: KBRuntimeOptionsRequest) -> Dict[str, Any]:
    """解析前端开关与运行时策略，返回生效模式。"""
    try:
        gateway = get_knowledge_gateway(storage)
        resolved = _resolve_kb_runtime_options_payload(
            gateway,
            remote_enabled=bool(request.remote_enabled),
            retrieval_mode=request.retrieval_mode,
            save_mode=request.save_mode,
        )
        _raise_for_kb_runtime_warning(resolved)
        return resolved
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving KB runtime options: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/search")
async def kb_search(request: KBSearchRequest) -> Dict[str, Any]:
    """统一知识库检索（本地/联合）。"""
    query = _require_kb_search_query(request.query)
    request_context = _build_kb_search_request_context(request)

    try:
        gateway = get_knowledge_gateway(storage)
        runtime_options, effective_mode = _resolve_kb_search_effective_mode(
            gateway,
            _as_str(request_context.get("retrieval_mode"), "local"),
        )
        payload = _execute_kb_search(
            gateway,
            request_context,
            query=query,
            effective_mode=effective_mode,
        )
        return _build_kb_search_response(
            payload,
            effective_mode=effective_mode,
            runtime_options=runtime_options,
        )
    except Exception as e:
        logger.error(f"Error searching KB: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/from-analysis-session")
async def kb_from_analysis_session(request: KBFromAnalysisSessionRequest) -> Dict[str, Any]:
    """从 AI 分析会话生成知识草稿。"""
    session_id = _require_kb_analysis_session_id(request.analysis_session_id)

    try:
        session_store = get_ai_session_store(storage)
        session, messages = await _load_kb_analysis_session_payload(
            session_store=session_store,
            run_blocking=_run_blocking,
            session_id=session_id,
        )
        max_history_items = _resolve_kb_draft_max_history_items()
        merged_history_messages = _build_kb_merged_history_messages(
            messages,
            request.history,
            max_history_items=max_history_items,
            session_messages_to_history=_session_messages_to_conversation_history,
            normalize_history=_normalize_conversation_history,
            mask_payload=_mask_sensitive_payload,
            merge_history=_merge_conversation_history,
        )

        include_followup = bool(request.include_followup)
        llm_enabled = _is_llm_configured()
        llm_requested = bool(request.use_llm)
        draft_bundle = await _resolve_kb_draft_bundle(
            session=session,
            merged_history_messages=merged_history_messages,
            include_followup=include_followup,
            llm_enabled=llm_enabled,
            llm_requested=llm_requested,
            build_rule_based_kb_draft=_build_rule_based_kb_draft,
            build_kb_draft_quality=_build_kb_draft_quality,
            build_llm_kb_draft=_build_llm_kb_draft,
            logger=logger,
        )
        save_mode_effective = _resolve_kb_effective_save_mode(
            gateway=get_knowledge_gateway(storage),
            remote_enabled=bool(request.remote_enabled),
            save_mode=request.save_mode,
        )
        return _build_kb_from_analysis_response(
            draft_bundle=draft_bundle,
            save_mode_effective=save_mode_effective,
            llm_enabled=llm_enabled,
            llm_requested=llm_requested,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating KB draft from analysis session: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


@router.post("/kb/solutions/optimize")
async def optimize_kb_solution_content(request: KBSolutionOptimizeRequest) -> Dict[str, Any]:
    """优化知识库解决建议文本，输出标准规范格式。"""
    raw_content = _truncate_text(_as_str(request.content), 6000).strip()
    if not raw_content:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "content is required"})

    llm_enabled = _is_llm_configured()
    llm_requested = bool(request.use_llm)
    method = "rule-based"
    llm_fallback_reason = ""
    optimized_text = _format_solution_text_standard(
        raw_content,
        summary=request.summary,
        service_name=request.service_name,
        problem_type=request.problem_type,
        severity=request.severity,
    )

    if llm_enabled and llm_requested:
        prompt = (
            "你是 SRE 知识库编辑器。请把输入的“解决建议草稿”优化成可执行、可审计的标准规范文本。\n"
            "输出规则：\n"
            "1) 仅输出正文，不要 Markdown 代码块、不要额外解释；\n"
            "2) 严格包含以下分段标题：\n"
            "【目标】\n【问题上下文】\n【处理步骤】\n【验证方式】\n【回滚方案】\n【风险与注意】\n"
            "3) 【处理步骤】必须为编号列表，3-8 步，动词开头，可直接执行；\n"
            "4) 文本简洁、专业、避免空话，总长度控制在 1200 字以内。\n\n"
            f"服务: {_as_str(request.service_name, 'unknown')}\n"
            f"问题类型: {_as_str(request.problem_type, 'unknown')}\n"
            f"严重级别: {_normalize_kb_draft_severity(request.severity, default='medium')}\n"
            f"摘要: {_truncate_text(_as_str(request.summary), 300)}\n\n"
            "待优化草稿：\n"
            f"{_mask_sensitive_text(raw_content)}"
        )
        llm_timeout_seconds = max(5, int(_as_float(os.getenv("AI_KB_SOLUTION_OPTIMIZE_TIMEOUT_SECONDS", 40), 40)))
        try:
            llm_service = get_llm_service()
            llm_answer = await asyncio.wait_for(
                llm_service.chat(
                    message=prompt,
                    context={
                        "task": "kb_solution_optimize",
                        "service_name": _as_str(request.service_name),
                        "problem_type": _as_str(request.problem_type),
                        "severity": _normalize_kb_draft_severity(request.severity, default="medium"),
                    },
                ),
                timeout=llm_timeout_seconds,
            )
            text = _truncate_text(_as_str(llm_answer), 2000).strip()
            if text:
                optimized_text = text
                method = "llm"
            else:
                llm_fallback_reason = "llm_empty_response"
        except asyncio.TimeoutError:
            llm_fallback_reason = "llm_timeout"
        except Exception as e:
            logger.warning(f"KB solution optimize failed, fallback to rule-based: {e}")
            llm_fallback_reason = "llm_error"
    else:
        if llm_enabled and not llm_requested:
            llm_fallback_reason = "llm_disabled_by_user"
        elif not llm_enabled:
            llm_fallback_reason = "llm_unavailable"

    response = {
        "optimized_text": optimized_text,
        "method": method,
        "applied_style": "standard_kb_solution_v1",
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
    }
    if llm_fallback_reason:
        response["llm_fallback_reason"] = llm_fallback_reason
    return response


@router.patch("/cases/{case_id}/manual-remediation")
async def update_manual_remediation(case_id: str, request: ManualRemediationRequest) -> Dict[str, Any]:
    """更新人工修复步骤并写入验证结果。"""
    steps, notes, verification_result = _validate_manual_remediation_request(
        request,
        as_list=_as_list,
        as_str=_as_str,
    )

    try:
        from ai.similar_cases import get_case_store, Case

        case_store = get_case_store(storage)
        existing = case_store.get_case(case_id)
        if not existing:
            raise HTTPException(status_code=404, detail={"code": "KBR-005", "message": "case not found"})

        updated, knowledge_version, history_records = _prepare_manual_remediation_case_update(
            existing,
            request,
            steps,
            notes,
            verification_result,
            Case,
            as_list=_as_list,
            as_str=_as_str,
            as_float=_as_float,
            utc_now_iso=_utc_now_iso,
        )
        effective_save_mode, remote_result = _sync_manual_remediation_update(
            updated,
            request,
            gateway=get_knowledge_gateway(storage),
            as_str=_as_str,
            build_case_payload_for_remote=_build_case_payload_for_remote,
        )
        _apply_manual_remediation_sync_result(
            updated_case=updated,
            history_records=history_records,
            knowledge_version=knowledge_version,
            steps=steps,
            notes=notes,
            verification_result=verification_result,
            effective_save_mode=effective_save_mode,
            remote_result=remote_result,
            as_str=_as_str,
        )

        case_store.update_case(updated)
        remediation_change_summary = _build_manual_remediation_change_summary(
            existing,
            updated,
            steps,
            notes,
            verification_result,
            as_list=_as_list,
            as_str=_as_str,
        )
        _append_manual_remediation_change_history(
            case_store,
            updated,
            knowledge_version,
            remediation_change_summary,
            effective_save_mode,
            append_case_change_history=lambda store, case_id, payload: _case_store_append_change_history(
                store,
                case_id,
                payload,
                warn=logger.warning,
            ),
        )
        _metric_inc(KB_MANUAL_REMEDIATION_UPDATE_TOTAL)

        return _build_manual_remediation_response(
            updated,
            knowledge_version,
            effective_save_mode,
            remote_result,
            as_list=_as_list,
            as_str=_as_str,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating manual remediation: {e}")
        raise HTTPException(status_code=500, detail={"code": "KBR-010", "message": "Internal server error"})


async def _run_follow_up_analysis_core(
    request: FollowUpRequest,
    *,
    event_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """追问分析核心流程（供普通与流式接口复用）。"""
    question = _as_str(request.question)
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    timeout_profile = _resolve_followup_timeout_profile()
    deadline_ts = time.perf_counter() + float(timeout_profile["request_deadline_seconds"])
    safe_question = _mask_sensitive_text(question)
    analysis_context = _mask_sensitive_payload(request.analysis_context or {})
    runtime_lab_mode = _is_ai_runtime_lab_mode(analysis_context=analysis_context)
    show_thought = bool(getattr(request, "show_thought", False))
    thought_timeline: List[Dict[str, Any]] = []
    session_store = get_ai_session_store(storage)

    async def _emit_thought(
        *,
        phase: str,
        title: str,
        detail: str = "",
        status: str = "info",
        iteration: Optional[int] = None,
    ) -> None:
        safe_title = _as_str(title)
        if not safe_title:
            return
        payload: Dict[str, Any] = {
            "phase": _as_str(phase, "thought"),
            "status": _as_str(status, "info"),
            "title": safe_title,
            "detail": _as_str(detail),
            "timestamp": _utc_now_iso(),
        }
        if iteration is not None:
            payload["iteration"] = max(1, int(_as_float(iteration, 1)))
        thought_timeline.append(payload)
        if show_thought:
            await _emit_followup_event(
                event_callback,
                "thought",
                payload,
                logger=logger,
            )

    await _emit_followup_event(event_callback, "plan", {"stage": "session_prepare"}, logger=logger)
    analysis_session_id = _as_str(request.analysis_session_id) or _as_str(analysis_context.get("session_id"))
    analysis_session_id = await asyncio.wait_for(
        _ensure_followup_analysis_session(
            session_store=session_store,
            run_blocking=_run_blocking,
            analysis_session_id=analysis_session_id,
            analysis_context=analysis_context,
            question=question,
            extract_overview_summary=_extract_overview_summary,
            llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
        ),
        timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["session_prepare_timeout_seconds"])),
    )

    conversation_id = _as_str(request.conversation_id) or f"conv-{uuid.uuid4().hex[:12]}"
    if request.reset:
        _clear_conversation_history(conversation_id)

    history_timeout = False
    await _emit_followup_event(event_callback, "plan", {"stage": "history_load"}, logger=logger)
    try:
        history = await asyncio.wait_for(
            _build_followup_history(
                session_store=session_store,
                run_blocking=_run_blocking,
                analysis_session_id=analysis_session_id,
                request_history=request.history,
                conversation_id=conversation_id,
                normalize_conversation_history=_normalize_conversation_history,
                mask_sensitive_payload=_mask_sensitive_payload,
                get_conversation_history=_get_conversation_history,
                session_messages_to_conversation_history=_session_messages_to_conversation_history,
                merge_conversation_history=_merge_conversation_history,
            ),
            timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["history_load_timeout_seconds"])),
        )
    except asyncio.TimeoutError:
        history_timeout = True
        logger.warning(
            "Follow-up history load timeout (session_id=%s, timeout=%ss)",
            analysis_session_id,
            timeout_profile["history_load_timeout_seconds"],
        )
        history = []

    compacted_info = _compact_conversation_for_prompt(history)
    compacted_history = compacted_info.get("history", history)
    compacted_summary = _as_str(compacted_info.get("summary"))
    history_compacted = bool(compacted_info.get("compacted"))
    duplicate_question_turn: Dict[str, Any] = {}
    if runtime_lab_mode:
        duplicate_question_turn = _find_duplicate_question_turn(
            history=compacted_history if isinstance(compacted_history, list) else history,
            question=safe_question,
        )

    long_term_memory_timeout = False
    await _emit_followup_event(event_callback, "plan", {"stage": "long_term_memory"}, logger=logger)
    try:
        long_term_memory = await asyncio.wait_for(
            _build_followup_long_term_memory(
                session_store=session_store,
                run_blocking=_run_blocking,
                analysis_session_id=analysis_session_id,
                analysis_context=analysis_context,
                question=safe_question,
            ),
            timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["long_term_memory_timeout_seconds"])),
        )
    except asyncio.TimeoutError:
        long_term_memory_timeout = True
        logger.warning(
            "Follow-up long-term memory timeout (session_id=%s, timeout=%ss)",
            analysis_session_id,
            timeout_profile["long_term_memory_timeout_seconds"],
        )
        long_term_memory = {"enabled": False, "hits": 0, "summary": "", "items": []}

    long_term_memory_summary = _as_str(long_term_memory.get("summary"))
    long_term_memory_hits = int(_as_float(long_term_memory.get("hits"), 0))
    references = _build_followup_references(
        analysis_context,
        mask_sensitive_text=_mask_sensitive_text,
    )
    context_pills = _build_context_pills(
        analysis_context,
        analysis_session_id=analysis_session_id,
        extract_overview_summary=_extract_overview_summary,
        mask_sensitive_text=_mask_sensitive_text,
    )
    react_memory_timeout = False
    react_memory = {"enabled": True, "hits": 0, "next_actions": [], "failed_commands": [], "summary": ""}
    await _emit_followup_event(event_callback, "plan", {"stage": "react_memory_load"}, logger=logger)
    try:
        react_getter = getattr(session_store, "get_recent_assistant_messages_for_react", None)
        if callable(react_getter):
            stored_messages_for_react = await asyncio.wait_for(
                _run_blocking(react_getter, analysis_session_id, 12),
                timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["react_memory_timeout_seconds"])),
            )
        else:
            stored_messages_for_react = await asyncio.wait_for(
                _run_blocking(session_store.get_messages, analysis_session_id, 120),
                timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["react_memory_timeout_seconds"])),
            )
        react_memory = _build_followup_react_memory(stored_messages_for_react)
    except asyncio.TimeoutError:
        react_memory_timeout = True
        logger.warning(
            "Follow-up react memory load timeout (session_id=%s, timeout=%ss)",
            analysis_session_id,
            timeout_profile["react_memory_timeout_seconds"],
        )
    except Exception as exc:
        logger.warning("Follow-up react memory load failed (session_id=%s): %s", analysis_session_id, exc)

    runtime_thread_memory = {"enabled": True, "hits": 0, "summary": "", "facts": [], "failed_actions": [], "successful_actions": [], "approval_history": [], "user_constraints": []}
    try:
        runtime_service = get_agent_runtime_service(storage)
        runtime_runs = runtime_service.store.list_runs_by_thread(
            session_id=analysis_session_id,
            conversation_id=conversation_id,
            limit=6,
        )
        runtime_thread_memory = _build_followup_runtime_thread_memory(runtime_runs)
    except Exception as exc:
        logger.warning(
            "Follow-up runtime thread memory load failed (session_id=%s, conversation_id=%s): %s",
            analysis_session_id,
            conversation_id,
            exc,
        )

    runtime_thread_summary = _as_str(runtime_thread_memory.get("summary")).strip()
    if runtime_thread_summary:
        compacted_summary = (
            f"{compacted_summary}\n\nRuntime thread memory:\n{runtime_thread_summary}"
            if compacted_summary
            else f"Runtime thread memory:\n{runtime_thread_summary}"
        )[:2400]
    for failed_action in _as_list(runtime_thread_memory.get("failed_actions")):
        line = _as_str(failed_action).strip()
        if line and line not in _as_list(react_memory.get("failed_commands")):
            react_memory["failed_commands"] = _as_list(react_memory.get("failed_commands")) + [line]
    if runtime_thread_summary and not _as_str(react_memory.get("summary")).strip():
        react_memory["summary"] = runtime_thread_summary[:500]

    reflection_max_iterations = max(1, int(_as_float(os.getenv("AI_FOLLOWUP_REFLECTION_MAX_ITERATIONS", 3), 3)))
    subgoals = _build_followup_subgoals(
        safe_question,
        analysis_context,
        references,
    )
    reflection = _build_followup_reflection(
        subgoals,
        references,
        max_iterations=reflection_max_iterations,
    )
    reflection = _merge_reflection_with_react_memory(reflection, react_memory)
    planner_prompt = _build_followup_planner_prompt(subgoals, reflection)
    await _emit_followup_event(
        event_callback,
        "plan",
        {
            "stage": "planning_ready",
            "subgoals": subgoals,
            "reflection": reflection,
            "react_memory_hits": int(_as_float(react_memory.get("hits"), 0)),
        },
        logger=logger,
    )
    reflection_gaps = _as_list(reflection.get("gaps"))
    await _emit_thought(
        phase="plan",
        title=f"完成问题拆解，子目标 {len(subgoals)}",
        detail=f"待补证据点 {len(reflection_gaps)}" if reflection_gaps else "已进入回答生成阶段",
    )
    react_memory_hits = int(_as_float(react_memory.get("hits"), 0))
    if react_memory_hits > 0:
        _metric_inc(AI_FOLLOWUP_REACT_MEMORY_HITS_TOTAL, react_memory_hits)
    history, user_message, persist_user_message = _upsert_followup_user_message(
        history,
        safe_question,
        trim_conversation_history=_trim_conversation_history,
        utc_now_iso=_utc_now_iso,
    )

    llm_enabled = _is_llm_configured()
    llm_requested = bool(request.use_llm)
    token_budget = max(1000, int(os.getenv("AI_FOLLOWUP_TOKEN_BUDGET", "12000")))
    token_warn_threshold = max(100, int(os.getenv("AI_FOLLOWUP_TOKEN_WARN_THRESHOLD", "1500")))
    token_estimate = _estimate_token_usage(
        safe_question,
        compacted_history,
        compacted_summary,
        analysis_context,
        references,
    )
    token_remaining = token_budget - token_estimate
    token_warning = token_remaining < token_warn_threshold

    async def _stream_token_callback(chunk: str) -> None:
        masked_chunk = _mask_sensitive_text(_as_str(chunk))
        if not masked_chunk:
            return
        await _emit_followup_event(
            event_callback,
            "token",
            {"text": masked_chunk},
            logger=logger,
        )

    await _emit_followup_event(
        event_callback,
        "plan",
        {
            "stage": "llm_start",
            "llm_enabled": llm_enabled,
            "llm_requested": llm_requested,
            "token_warning": token_warning,
        },
        logger=logger,
    )
    await _emit_thought(
        phase="thought",
        title=f"开始生成回答（{'LLM' if llm_requested and llm_enabled else '规则模式'}）",
        detail="上下文较长，已触发压缩预算提示" if token_warning else "",
        status="warning" if token_warning else "info",
    )

    answer_generation_timeout = False
    try:
        answer_bundle = await asyncio.wait_for(
            _resolve_followup_answer_bundle(
                safe_question=safe_question,
                analysis_context=analysis_context,
                compacted_history=compacted_history,
                compacted_summary=compacted_summary,
                references=references,
                subgoals=subgoals,
                reflection=reflection,
                planner_prompt=planner_prompt,
                long_term_memory=long_term_memory,
                llm_enabled=llm_enabled,
                llm_requested=llm_requested,
                token_budget=token_budget,
                token_warning=token_warning,
                llm_timeout_seconds=timeout_profile["llm_total_timeout_seconds"],
                llm_first_token_timeout_seconds=timeout_profile["llm_first_token_timeout_seconds"],
                analysis_session_id=analysis_session_id,
                resolve_followup_engine=_resolve_followup_engine,
                run_followup_langchain_fn=run_followup_langchain,
                get_llm_service_fn=get_llm_service,
                build_followup_fallback_answer=_build_followup_fallback_answer,
                build_followup_response_instruction=_build_followup_response_instruction,
                stream_token_callback=_stream_token_callback if callable(event_callback) else None,
                logger=logger,
            ),
            timeout=_remaining_timeout(deadline_ts),
        )
    except asyncio.TimeoutError:
        answer_generation_timeout = True
        logger.warning("Follow-up answer stage timed out (session_id=%s)", analysis_session_id)
        await _emit_thought(
            phase="thought",
            title="回答生成超时，自动降级到规则模式",
            status="warning",
        )
        answer_bundle = {
            "answer": _build_followup_fallback_answer(
                safe_question,
                analysis_context,
                fallback_reason="llm_timeout",
                reflection=reflection,
            ),
            "analysis_method": "rule-based",
            "llm_timeout_fallback": True,
            "followup_engine": _resolve_followup_engine(),
            "langchain_actions": [],
        }

    method = _as_str(answer_bundle.get("analysis_method"), "rule-based")
    llm_timeout_fallback = bool(answer_bundle.get("llm_timeout_fallback"))
    followup_engine = _as_str(answer_bundle.get("followup_engine"))
    langchain_actions = _as_list(answer_bundle.get("langchain_actions"))
    masked_answer = _mask_sensitive_text(_as_str(answer_bundle.get("answer"), "暂无回答"))
    answer_missing_evidence = [
        _as_str(item).strip()
        for item in _as_list(answer_bundle.get("missing_evidence"))
        if _as_str(item).strip()
    ]
    reflection_gaps = [
        _as_str(item).strip()
        for item in _as_list(reflection.get("gaps"))
        if _as_str(item).strip()
    ]
    evidence_gap_queue_for_summary: List[str] = []
    seen_gap_items: set[str] = set()
    for item in answer_missing_evidence + reflection_gaps:
        if not item:
            continue
        normalized = item[:220]
        if normalized in seen_gap_items:
            continue
        seen_gap_items.add(normalized)
        evidence_gap_queue_for_summary.append(normalized)
    evidence_gap_queue_for_execution: List[str] = []
    seen_exec_gap_items: set[str] = set()
    for item in answer_missing_evidence:
        normalized = _as_str(item).strip()[:220]
        if not normalized or normalized in seen_exec_gap_items:
            continue
        seen_exec_gap_items.add(normalized)
        evidence_gap_queue_for_execution.append(normalized)
    answer_summary_seed = _as_str(answer_bundle.get("analysis_summary")).strip()
    if not answer_summary_seed:
        answer_summary_seed = _as_str(masked_answer).strip().splitlines()[0][:280] if _as_str(masked_answer).strip() else ""
    initial_summary_parts: List[str] = []
    if answer_summary_seed:
        initial_summary_parts.append(f"当前结论：{answer_summary_seed[:180]}")
    if evidence_gap_queue_for_summary:
        initial_summary_parts.append(f"待补证据：{'；'.join(evidence_gap_queue_for_summary[:4])}")
    else:
        initial_summary_parts.append("当前证据缺口为空，优先输出结论。")
    await _emit_thought(
        phase="summary",
        title="阶段总结：先形成结论，再按缺口补证据",
        detail="；".join(initial_summary_parts)[:480],
        status="info",
    )
    followup_actions = _build_followup_actions(
        question=safe_question,
        answer=masked_answer,
        reflection=reflection,
        langchain_actions=langchain_actions,
        mask_text=_mask_sensitive_text,
    )
    followup_actions = _prioritize_followup_actions_with_react_memory(
        actions=followup_actions,
        react_memory=react_memory,
        max_items=max(1, int(_as_float(os.getenv("AI_FOLLOWUP_ACTION_MAX_ITEMS", 8), 8))),
        max_append=max(0, int(_as_float(os.getenv("AI_FOLLOWUP_REACT_MEMORY_MAX_APPEND", 2), 2))),
    )

    assistant_message_id = f"msg-{uuid.uuid4().hex[:12]}"
    await _emit_followup_event(
        event_callback,
        "action",
        {
            "message_id": assistant_message_id,
            "actions": followup_actions,
        },
        logger=logger,
    )
    await _emit_thought(
        phase="action",
        title=f"生成执行计划 {len(followup_actions)} 项",
        detail=(
            "仅查询类命令会自动执行，写命令保留人工确认。"
            if evidence_gap_queue_for_execution
            else "未提供结构化缺口，按候选查询动作执行并在每轮后复盘。"
        ),
    )
    executed_commands_set = {
        _normalize_followup_command_line(item)
        for item in _as_list(analysis_context.get("_runtime_executed_commands"))
        if _normalize_followup_command_line(item)
    }
    prior_action_observations = [
        item
        for item in _as_list(analysis_context.get("_runtime_prior_action_observations"))
        if isinstance(item, dict)
    ]
    react_exec_bundle = await _run_followup_auto_exec_react_loop(
        session_id=analysis_session_id,
        message_id=assistant_message_id,
        actions=followup_actions,
        analysis_context=analysis_context,
        allow_auto_exec_readonly=bool(getattr(request, "auto_exec_readonly", True)),
        executed_commands=executed_commands_set,
        initial_action_observations=prior_action_observations,
        initial_evidence_gaps=evidence_gap_queue_for_execution,
        initial_summary=answer_summary_seed,
        emit_iteration_thoughts=bool(show_thought),
        run_blocking=_run_blocking,
        build_react_loop_fn=_build_followup_react_loop,
        event_callback=event_callback,
        logger=logger,
    )
    promoted_actions = [
        item
        for item in _as_list(react_exec_bundle.get("actions"))
        if isinstance(item, dict)
    ]
    if promoted_actions:
        followup_actions = promoted_actions
    action_observations = _as_list(react_exec_bundle.get("action_observations"))
    react_loop = react_exec_bundle.get("react_loop") if isinstance(react_exec_bundle.get("react_loop"), dict) else {}
    react_iterations = _as_list(react_exec_bundle.get("react_iterations"))
    react_replan = react_loop.get("replan") if isinstance(react_loop.get("replan"), dict) else {}
    react_need_replan = bool(react_replan.get("needed"))
    await _emit_thought(
        phase="replan",
        title="闭环评估需继续重规划" if react_need_replan else "闭环评估已收敛",
        detail=_as_str((react_loop or {}).get("summary")),
        status="warning" if react_need_replan else "success",
    )

    observations_by_action = {
        _as_str(item.get("action_id")): item
        for item in action_observations
        if isinstance(item, dict) and _as_str(item.get("action_id"))
    }
    for action in followup_actions:
        action_id = _as_str((action or {}).get("id"))
        if not action_id:
            continue
        observation = observations_by_action.get(action_id)
        if not isinstance(observation, dict):
            continue
        action["observation"] = {
            "status": _as_str(observation.get("status")),
            "exit_code": int(_as_float(observation.get("exit_code"), 0)),
            "timed_out": bool(observation.get("timed_out")),
            "message": _as_str(observation.get("message")),
        }

    auto_exec_success = 0
    auto_exec_failed = 0
    for item in action_observations:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("auto_executed")):
            continue
        status = _as_str(item.get("status")).lower()
        if status == "executed" and int(_as_float(item.get("exit_code"), 0)) == 0:
            auto_exec_success += 1
        else:
            auto_exec_failed += 1
    if auto_exec_success > 0:
        _metric_inc(AI_FOLLOWUP_AUTO_EXEC_SUCCESS_TOTAL, auto_exec_success)
    if auto_exec_failed > 0:
        _metric_inc(AI_FOLLOWUP_AUTO_EXEC_FAILED_TOTAL, auto_exec_failed)
    if bool((react_loop.get("replan") or {}).get("needed")):
        _metric_inc(AI_FOLLOWUP_REACT_REPLAN_TOTAL)
    if any(
        normalize_followup_reason_code((item if isinstance(item, dict) else {}).get("reason"))
        == "no_executable_query_candidates"
        for item in _as_list((react_replan or {}).get("items"))
    ):
        _metric_inc(AI_FOLLOWUP_NO_EXECUTABLE_QUERY_CANDIDATES_TOTAL)

    for action in followup_actions:
        action_dict = action if isinstance(action, dict) else {}
        reason_code = normalize_followup_reason_code(action_dict.get("reason"))
        if not bool(action_dict.get("executable")) and reason_code in _FOLLOWUP_SPEC_COMPILE_FAILURE_REASON_CODES:
            _metric_inc(
                AI_FOLLOWUP_ACTION_SPEC_COMPILE_FAILED_TOTAL,
                labels={
                    "reason": reason_code,
                    "reason_group": map_followup_reason_group(reason_code),
                },
            )
        repair_reason_code = normalize_followup_reason_code(action_dict.get("spec_repair_from_reason"))
        if bool(action_dict.get("spec_repaired")) and repair_reason_code in _FOLLOWUP_GLUE_REASON_CODES:
            _metric_inc(
                AI_FOLLOWUP_GLUE_REPAIR_SUCCESS_TOTAL,
                labels={
                    "reason": repair_reason_code,
                    "reason_group": map_followup_reason_group(repair_reason_code),
                },
            )

    for item in action_observations:
        observation = item if isinstance(item, dict) else {}
        if _as_str(observation.get("status")).lower() != "semantic_incomplete":
            continue
        reason_code = normalize_followup_reason_code(
            observation.get("message")
            or observation.get("reason")
        )
        _metric_inc(
            AI_FOLLOWUP_SEMANTIC_INCOMPLETE_TOTAL,
            labels={
                "reason": reason_code,
                "reason_group": map_followup_reason_group(reason_code),
            },
        )

    masked_answer = _mask_sensitive_text(
        _append_followup_react_summary(
            answer=_stabilize_followup_answer_when_plan_is_non_executable(
                answer=masked_answer,
                actions=followup_actions,
                action_observations=action_observations,
                react_loop=react_loop,
            ),
            react_loop=react_loop,
            actions=followup_actions,
        )
    )

    visible_thoughts = thought_timeline[-20:] if show_thought else []

    assistant_message = {
        "message_id": assistant_message_id,
        "role": "assistant",
        "content": masked_answer,
        "timestamp": _utc_now_iso(),
        "metadata": {
            "references": references,
            "context_pills": context_pills,
            "token_budget": token_budget,
            "token_estimate": token_estimate,
            "token_remaining": token_remaining,
            "token_warning": token_warning,
            "history_compacted": history_compacted,
            "llm_timeout_fallback": llm_timeout_fallback,
            "followup_engine": followup_engine,
            "long_term_memory_enabled": bool(long_term_memory.get("enabled")),
            "long_term_memory_hits": long_term_memory_hits,
            "long_term_memory_summary": long_term_memory_summary[:500],
            "subgoals": subgoals,
            "reflection": reflection,
            "actions": followup_actions,
            "action_observations": action_observations,
            "react_loop": react_loop,
            "react_iterations": react_iterations,
            "react_memory": react_memory,
            "runtime_thread_memory": runtime_thread_memory,
            "thoughts": visible_thoughts,
            "timeout_profile": timeout_profile,
            "history_timeout": history_timeout,
            "long_term_memory_timeout": long_term_memory_timeout,
            "react_memory_timeout": react_memory_timeout,
            "answer_generation_timeout": answer_generation_timeout,
        },
    }
    try:
        response_history = await asyncio.wait_for(
            _persist_followup_messages_and_history(
                session_store=session_store,
                run_blocking=_run_blocking,
                analysis_session_id=analysis_session_id,
                history=history,
                conversation_id=conversation_id,
                user_message=user_message,
                persist_user_message=persist_user_message,
                assistant_message=assistant_message,
                trim_conversation_history=_trim_conversation_history,
                set_conversation_history=_set_conversation_history,
            ),
            timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["persist_timeout_seconds"])),
        )
    except asyncio.TimeoutError:
        logger.warning("Follow-up persistence timeout (session_id=%s)", analysis_session_id)
        response_history = history + [assistant_message]
    try:
        await asyncio.wait_for(
            _update_followup_session_summary(
                session_store=session_store,
                run_blocking=_run_blocking,
                analysis_session_id=analysis_session_id,
                analysis_context=analysis_context,
                analysis_method=method,
                llm_provider=_as_str(os.getenv("LLM_PROVIDER", "")),
            ),
            timeout=min(_remaining_timeout(deadline_ts), float(timeout_profile["persist_timeout_seconds"])),
        )
    except asyncio.TimeoutError:
        logger.warning("Follow-up summary update timeout (session_id=%s)", analysis_session_id)

    return {
        "analysis_session_id": analysis_session_id,
        "conversation_id": conversation_id,
        "analysis_method": method,
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
        "answer": masked_answer,
        "history": response_history,
        "references": references,
        "context_pills": context_pills,
        "history_compacted": history_compacted,
        "conversation_summary": compacted_summary,
        "token_budget": token_budget,
        "token_estimate": token_estimate,
        "token_remaining": token_remaining,
        "token_warning": token_warning,
        "llm_timeout_fallback": llm_timeout_fallback,
        "followup_engine": followup_engine,
        "long_term_memory_enabled": bool(long_term_memory.get("enabled")),
        "long_term_memory_hits": long_term_memory_hits,
        "long_term_memory_summary": long_term_memory_summary,
        "subgoals": subgoals,
        "reflection": reflection,
        "actions": followup_actions,
        "action_observations": action_observations,
        "react_loop": react_loop,
        "react_iterations": react_iterations,
        "react_memory": react_memory,
        "runtime_thread_memory": runtime_thread_memory,
        "thoughts": visible_thoughts,
        "timeout_profile": timeout_profile,
        "history_timeout": history_timeout,
        "long_term_memory_timeout": long_term_memory_timeout,
        "react_memory_timeout": react_memory_timeout,
        "answer_generation_timeout": answer_generation_timeout,
        "executed_commands": sorted(executed_commands_set),
    }


@router.post("/follow-up")
async def follow_up_analysis(request: FollowUpRequest) -> Dict[str, Any]:
    """追问分析接口，支持会话上下文管理。"""
    timeout_profile = _resolve_followup_timeout_profile()
    try:
        return await asyncio.wait_for(
            _run_follow_up_analysis_core(request),
            timeout=float(timeout_profile["request_deadline_seconds"]),
        )
    except asyncio.TimeoutError:
        logger.warning("Follow-up request deadline exceeded")
        raise HTTPException(status_code=504, detail="follow-up request timeout")


@router.post("/follow-up/stream")
async def follow_up_analysis_stream(request: FollowUpRequest) -> StreamingResponse:
    """追问分析流式接口（SSE）。"""
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _queue_event(event_name: str, payload: Dict[str, Any]) -> None:
        await event_queue.put((event_name, payload if isinstance(payload, dict) else {}))

    async def _runner() -> None:
        try:
            result = await _run_follow_up_analysis_core(
                request,
                event_callback=_queue_event,
            )
            await event_queue.put(("final", result))
        except HTTPException as exc:
            await event_queue.put(
                (
                    "error",
                    {
                        "status_code": int(exc.status_code),
                        "detail": exc.detail,
                    },
                )
            )
        except Exception as exc:
            logger.error(f"Error in follow-up stream: {exc}")
            await event_queue.put(("error", {"status_code": 500, "detail": "Internal server error"}))
        finally:
            await event_queue.put(None)

    runner_task = asyncio.create_task(_runner())

    async def _event_generator():
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                event_name, payload = item
                yield _format_sse_event(_as_str(event_name, "message"), payload if isinstance(payload, dict) else {})
        finally:
            if not runner_task.done():
                runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner_task

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_event_generator(), media_type="text/event-stream", headers=headers)


@router.post("/v2/follow-up")
async def follow_up_analysis_v2(request: FollowUpRequest) -> Dict[str, Any]:
    """追问分析 v2：事件驱动 agent 协议（非流式调试模式）。"""
    timeout_profile = _resolve_followup_timeout_profile()
    events: List[Dict[str, Any]] = []

    async def _collector(event_name: str, payload: Dict[str, Any]) -> None:
        events.append(
            {
                "event": _as_str(event_name),
                "data": payload if isinstance(payload, dict) else {},
            }
        )

    try:
        result = await asyncio.wait_for(
            run_followup_v2_adapter(
                request=request,
                run_followup_core=_run_follow_up_analysis_core,
                emit_v2_event=_collector,
                precheck_command=precheck_controlled_command,
            ),
            timeout=float(timeout_profile["request_deadline_seconds"]),
        )
        result["protocol_version"] = "v2"
        result["agent_events"] = events[-200:]
        return result
    except asyncio.TimeoutError:
        logger.warning("Follow-up v2 request deadline exceeded")
        raise HTTPException(status_code=504, detail="follow-up v2 request timeout")


@router.post("/v2/follow-up/stream")
async def follow_up_analysis_stream_v2(request: FollowUpRequest) -> StreamingResponse:
    """追问分析 v2 SSE：Plan/Act/Observe/Replan 事件流。"""
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _queue_event(event_name: str, payload: Dict[str, Any]) -> None:
        await event_queue.put((event_name, payload if isinstance(payload, dict) else {}))

    async def _runner() -> None:
        timeout_profile = _resolve_followup_timeout_profile()
        try:
            await asyncio.wait_for(
                run_followup_v2_adapter(
                    request=request,
                    run_followup_core=_run_follow_up_analysis_core,
                    emit_v2_event=_queue_event,
                    precheck_command=precheck_controlled_command,
                ),
                timeout=float(timeout_profile["request_deadline_seconds"]),
            )
        except HTTPException as exc:
            await event_queue.put(
                (
                    "error",
                    {
                        "status_code": int(exc.status_code),
                        "detail": exc.detail,
                    },
                )
            )
        except asyncio.TimeoutError:
            await event_queue.put(
                (
                    "error",
                    {
                        "status_code": 504,
                        "detail": "follow-up v2 request timeout",
                    },
                )
            )
        except Exception as exc:
            logger.error(f"Error in follow-up v2 stream: {exc}")
            await event_queue.put(("error", {"status_code": 500, "detail": "Internal server error"}))
        finally:
            await event_queue.put(None)

    runner_task = asyncio.create_task(_runner())

    async def _event_generator():
        try:
            while True:
                item = await event_queue.get()
                if item is None:
                    break
                event_name, payload = item
                yield _format_sse_event(_as_str(event_name, "message"), payload if isinstance(payload, dict) else {})
        finally:
            if not runner_task.done():
                runner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner_task

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_event_generator(), media_type="text/event-stream", headers=headers)


class TraceAnalysisRequest(BaseModel):
    """Trace 分析请求"""
    trace_id: str


@router.post("/trace/analyze")
async def analyze_trace_detailed(request: TraceAnalysisRequest) -> Dict[str, Any]:
    """
    详细分析 Trace

    分析调用链的完整信息，包括：
    - 性能瓶颈
    - 错误节点
    - 根因分析
    - 优化建议
    """
    try:
        from ai.trace_analyzer import get_trace_analyzer

        analyzer = get_trace_analyzer(storage)
        result = await _run_blocking(analyzer.analyze_trace, request.trace_id)
        payload = {
            "trace_id": result.trace_id,
            "total_duration_ms": result.total_duration_ms,
            "service_count": result.service_count,
            "span_count": result.span_count,
            "root_cause_spans": result.root_cause_spans,
            "bottleneck_spans": result.bottleneck_spans,
            "error_spans": result.error_spans,
            "recommendations": result.recommendations,
            "service_timeline": result.service_timeline,
            "critical_path": result.critical_path,
        }
        session_id = await _persist_analysis_session(
            analysis_type="trace",
            service_name="",
            input_text=request.trace_id,
            trace_id=request.trace_id,
            context={"trace_id": request.trace_id, "mode": "detailed"},
            result=payload,
            source="api:/trace/analyze",
        )
        if session_id:
            payload["session_id"] = session_id
        return payload

    except Exception as e:
        logger.error(f"Error analyzing trace: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/trace/{trace_id}/visualization")
async def get_trace_visualization(trace_id: str) -> Dict[str, Any]:
    """
    获取 Trace 可视化数据

    返回用于前端渲染调用链图的数据
    """
    try:
        from ai.trace_analyzer import get_trace_analyzer

        analyzer = get_trace_analyzer(storage)
        result = await _run_blocking(analyzer.get_trace_visualization_data, trace_id)

        return result

    except Exception as e:
        logger.error(f"Error getting trace visualization: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/health")
async def health_check():
    """健康检查"""
    llm_configured = _is_llm_configured()
    
    return {
        "status": "healthy",
        "service": "ai-service",
        "analyzer": "ready",
        "llm_enabled": llm_configured,
        "llm_provider": os.getenv("LLM_PROVIDER", "openai"),
        "llm_model": os.getenv("LLM_MODEL", "gpt-4"),
        "followup_engine": _resolve_followup_engine(),
    }
