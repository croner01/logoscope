"""
AI agent runtime service.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

from ai.agent_runtime import event_protocol
from ai.agent_runtime.command_bridge import (
    bridge_exec_run_stream_to_runtime,
    build_approval_required_payload,
)
from ai.agent_runtime.exec_client import ExecServiceClientError, cancel_command_run, create_command_run
from ai.agent_runtime.models import AgentRun, RunEvent, build_id, utc_now_iso
from ai.agent_runtime.recovery import attempt_command_recovery
from ai.agent_runtime.status import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_WAITING_USER_INPUT,
    is_terminal_run_status,
)
from ai.agent_runtime.store import AgentRuntimeStore
from ai.agent_runtime.timeout_recovery import attempt_timeout_recovery
from ai.agent_runtime.user_question_adapter import build_business_question
from ai.followup_command import (
    _is_truthy_env,
    _normalize_followup_command_line,
    _repair_clickhouse_query_text,
    _resolve_followup_command_meta,
)
from ai.followup_command_spec import (
    build_command_spec_self_repair_payload,
    compile_followup_command_spec,
    normalize_followup_command_spec,
    normalize_followup_reason_code,
)
from ai.json_dict_helpers import _parse_llm_json_dict
from ai.llm_service import get_llm_service


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _require_spec_for_repair_enabled() -> bool:
    return _is_truthy_env("AI_FOLLOWUP_COMMAND_REQUIRE_SPEC_FOR_REPAIR", False)


def _require_structured_actions_enabled() -> bool:
    return _is_truthy_env("AI_RUNTIME_REQUIRE_STRUCTURED_ACTIONS", True)


def _diagnosis_contract_gate_enabled() -> bool:
    return _is_truthy_env("AI_RUNTIME_DIAGNOSIS_CONTRACT_ENFORCED", True)


def _sql_llm_repair_enabled() -> bool:
    return _is_truthy_env("AI_RUNTIME_SQL_LLM_REPAIR_ENABLED", True)


_AI_RUNTIME_LAB_PROFILE = "ai_runtime_lab"
_NON_RECOVERABLE_STRUCTURED_FAILURE_CODES = {
    "glued_sql_tokens",
    "unsupported_clickhouse_readonly_query",
    "clickhouse_multi_statement_not_allowed",
    "pod_selector_requires_shell",
}


def _normalize_diagnosis_contract(raw: Any) -> Dict[str, Any]:
    safe = raw if isinstance(raw, dict) else {}

    def _normalize_list(value: Any, max_items: int = 8) -> List[str]:
        values: List[str] = []
        for item in _as_list(value):
            text = _as_str(item).strip()
            if not text or text in values:
                continue
            values.append(text)
            if len(values) >= max_items:
                break
        return values

    return {
        "fault_summary": _as_str(safe.get("fault_summary")).strip(),
        "evidence_gaps": _normalize_list(safe.get("evidence_gaps"), max_items=8),
        "execution_plan": _normalize_list(safe.get("execution_plan"), max_items=8),
        "why_command_needed": _as_str(safe.get("why_command_needed")).strip(),
    }


def _diagnosis_contract_missing_fields(contract: Any) -> List[str]:
    safe = _normalize_diagnosis_contract(contract)
    missing: List[str] = []
    if not _as_str(safe.get("fault_summary")).strip():
        missing.append("fault_summary")
    if len(_as_list(safe.get("evidence_gaps"))) <= 0:
        missing.append("evidence_gaps")
    if len(_as_list(safe.get("execution_plan"))) <= 0:
        missing.append("execution_plan")
    if not _as_str(safe.get("why_command_needed")).strip():
        missing.append("why_command_needed")
    return missing


def _diagnosis_contract_missing_fields_text(missing_fields: List[str]) -> str:
    label_map = {
        "fault_summary": "fault_summary（故障结论）",
        "evidence_gaps": "evidence_gaps（证据缺口）",
        "execution_plan": "execution_plan（执行计划）",
        "why_command_needed": "why_command_needed（命令必要性）",
    }
    labels: List[str] = []
    for field in missing_fields:
        safe_field = _as_str(field).strip()
        if not safe_field:
            continue
        labels.append(label_map.get(safe_field, safe_field))
    if not labels:
        return "diagnosis_contract 必填字段"
    return "、".join(labels)


def _parse_diagnosis_contract_from_user_text(text: str) -> Dict[str, Any]:
    safe_text = _as_str(text).strip()
    if not safe_text:
        return {}
    if safe_text.startswith("{") and safe_text.endswith("}"):
        try:
            parsed = json.loads(safe_text)
            if isinstance(parsed, dict):
                return _normalize_diagnosis_contract(parsed)
        except Exception:
            return {}

    parsed_payload: Dict[str, Any] = {}
    for line in safe_text.splitlines():
        normalized_line = _as_str(line).strip()
        if not normalized_line:
            continue
        for sep in (":", "："):
            if sep not in normalized_line:
                continue
            key_raw, value_raw = normalized_line.split(sep, 1)
            key = _as_str(key_raw).strip().lower()
            value = _as_str(value_raw).strip()
            if not value:
                continue
            if key in {"fault_summary", "fault", "summary", "故障结论", "故障摘要"}:
                parsed_payload["fault_summary"] = value
            elif key in {"why_command_needed", "why", "命令必要性", "为何要执行命令"}:
                parsed_payload["why_command_needed"] = value
            elif key in {"evidence_gaps", "gaps", "证据缺口"}:
                parsed_payload["evidence_gaps"] = [item.strip() for item in re.split(r"[;,；，]", value) if item.strip()]
            elif key in {"execution_plan", "plan", "执行计划"}:
                parsed_payload["execution_plan"] = [item.strip() for item in re.split(r"[;,；，]", value) if item.strip()]
            break
    return _normalize_diagnosis_contract(parsed_payload)


def _contains_backend_unavailable_error(text: str) -> bool:
    lowered = _as_str(text).lower()
    if not lowered:
        return False
    markers = (
        "curl: (7)",
        "failed to connect to",
        "could not connect to server",
        "connection refused",
        "connection timed out",
        "operation timed out",
        "temporary failure in name resolution",
        "name or service not known",
        "backend unavailable",
        "exec-service unavailable",
    )
    return any(marker in lowered for marker in markers)


def _normalize_command_for_history(command: Any) -> str:
    text = _as_str(command).strip()
    if not text:
        return ""
    return " ".join(text.split())


def _build_command_fingerprint(command: str, purpose: str, action_id: str) -> str:
    del action_id
    payload = {
        "command": _normalize_command_for_history(command),
        "purpose": _as_str(purpose).strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _build_attempt_fingerprint(command: str, purpose: str, action_id: str) -> str:
    payload = {
        "command": _normalize_command_for_history(command),
        "purpose": _as_str(purpose).strip(),
        "action_id": _as_str(action_id).strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _resolve_command_target(command_spec: Optional[Dict[str, Any]]) -> Dict[str, str]:
    safe_spec = command_spec if isinstance(command_spec, dict) else {}
    safe_args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
    target_kind = _as_str(
        safe_args.get("target_kind")
        or safe_spec.get("target_kind")
    ).strip()
    target_identity = _as_str(
        safe_args.get("target_identity")
        or safe_spec.get("target_identity")
    ).strip()
    return {
        "target_kind": target_kind,
        "target_identity": target_identity,
    }


def _build_command_execution_key(
    *,
    run_id: str,
    action_id: str,
    command: str,
    purpose: str,
    target_identity: str,
) -> str:
    payload = {
        "run_id": _as_str(run_id).strip(),
        "action_id": _as_str(action_id).strip(),
        "command": _normalize_command_for_history(command),
        "purpose": _as_str(purpose).strip(),
        "target_identity": _as_str(target_identity).strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _normalize_command_run_index(value: Any, *, max_items: int = 300) -> Dict[str, Dict[str, Any]]:
    safe_index: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, dict):
        return safe_index
    for raw_key, raw_entry in value.items():
        key = _as_str(raw_key).strip()
        if not key or not isinstance(raw_entry, dict):
            continue
        safe_index[key] = dict(raw_entry)
    if len(safe_index) <= max(1, int(max_items or 300)):
        return safe_index
    ordered = sorted(
        safe_index.items(),
        key=lambda item: _as_str((item[1] or {}).get("last_updated_at") or (item[1] or {}).get("created_at")),
        reverse=True,
    )
    return {key: entry for key, entry in ordered[: max(1, int(max_items or 300))]}


def _upsert_command_run_index(
    existing: Any,
    *,
    command_execution_key: str,
    entry: Dict[str, Any],
    max_items: int = 300,
) -> Dict[str, Dict[str, Any]]:
    safe_key = _as_str(command_execution_key).strip()
    if not safe_key:
        return _normalize_command_run_index(existing, max_items=max_items)
    index = _normalize_command_run_index(existing, max_items=max_items * 2)
    previous = index.get(safe_key) if isinstance(index.get(safe_key), dict) else {}
    merged = {
        **previous,
        **(entry if isinstance(entry, dict) else {}),
    }
    merged["last_updated_at"] = _as_str(merged.get("last_updated_at")).strip() or utc_now_iso()
    index[safe_key] = merged
    if len(index) <= max(1, int(max_items or 300)):
        return index
    ordered = sorted(
        index.items(),
        key=lambda item: _as_str((item[1] or {}).get("last_updated_at") or (item[1] or {}).get("created_at")),
        reverse=True,
    )
    return {key: item for key, item in ordered[: max(1, int(max_items or 300))]}


def _build_command_plan_detail(
    *,
    command: str,
    purpose: str,
    reason: str,
    expected_outcome: str,
) -> str:
    safe_command = _normalize_command_for_history(command)
    safe_purpose = _as_str(purpose).strip() or "补齐当前证据缺口并推进排障"
    safe_reason = _as_str(reason).strip() or "该命令与当前排障目标匹配，可先补齐关键事实。"
    safe_expected = _as_str(expected_outcome).strip() or "输出可用于判断是否进入下一步。"
    lines = [
        "执行前计划：",
        f"1. 计划命令：{safe_command}",
        f"2. 执行目的：{safe_purpose}",
        f"3. 执行原因：{safe_reason}",
        f"4. 预期结果：{safe_expected}",
    ]
    return "\n".join(lines)


def _merge_unique_text_items(existing: Any, value: str, *, max_items: int = 200) -> List[str]:
    items = [_as_str(item).strip() for item in _as_list(existing)]
    dedup: List[str] = []
    seen = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    safe_value = _as_str(value).strip()
    if safe_value and safe_value not in seen:
        dedup.append(safe_value)
    return dedup[-max(1, int(max_items or 200)) :]


def _merge_timeout_recovery_history(existing: Any, entry: Optional[Dict[str, Any]], *, max_items: int = 20) -> List[Dict[str, Any]]:
    items = [item for item in _as_list(existing) if isinstance(item, dict)]
    dedup: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        match_key = _as_str(item.get("match_key")).strip()
        key = match_key or json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(dict(item))
    if isinstance(entry, dict) and entry:
        match_key = _as_str(entry.get("match_key")).strip()
        key = match_key or json.dumps(entry, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            dedup.append(dict(entry))
    return dedup[-max(1, int(max_items or 20)) :]


class AgentRuntimeService:
    """Coordinates run lifecycle and event fan-out."""

    def __init__(self, storage_adapter: Any = None):
        self.store = AgentRuntimeStore(storage_adapter=storage_adapter)
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}
        self._background_tasks: Dict[str, List[asyncio.Task]] = {}
        self._pending_action_timers: Dict[str, asyncio.Task] = {}

    def attach_storage(self, storage_adapter: Any) -> None:
        self.store.attach_storage(storage_adapter)

    def shutdown(self) -> None:
        with self._lock:
            background_tasks = [task for tasks in self._background_tasks.values() for task in tasks]
            self._background_tasks.clear()
            approval_timers = list(self._pending_action_timers.values())
            self._pending_action_timers.clear()
            for queues in self._subscribers.values():
                for queue in queues:
                    try:
                        queue.put_nowait(None)
                    except Exception:
                        continue
            self._subscribers.clear()
        for task in [*background_tasks, *approval_timers]:
            if not task.done():
                task.cancel()

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.setdefault(_as_str(run_id), []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            queues = self._subscribers.get(_as_str(run_id), [])
            self._subscribers[_as_str(run_id)] = [item for item in queues if item is not queue]
            if not self._subscribers[_as_str(run_id)]:
                self._subscribers.pop(_as_str(run_id), None)

    def _publish_event(self, event: RunEvent) -> None:
        with self._lock:
            queues = list(self._subscribers.get(event.run_id, []))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:
                continue

    def append_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> RunEvent:
        event = RunEvent(
            run_id=_as_str(run_id),
            seq=self.store.get_next_seq(run_id),
            event_type=_as_str(event_type),
            payload=payload if isinstance(payload, dict) else {},
        )
        self.store.append_event(event)
        self._publish_event(event)
        return event

    def _register_background_task(self, run_id: str, task: asyncio.Task) -> None:
        safe_run_id = _as_str(run_id)
        with self._lock:
            self._background_tasks.setdefault(safe_run_id, []).append(task)

        def _cleanup(done_task: asyncio.Task, *, rid: str = safe_run_id) -> None:
            with self._lock:
                tasks = self._background_tasks.get(rid, [])
                self._background_tasks[rid] = [item for item in tasks if item is not done_task]
                if not self._background_tasks[rid]:
                    self._background_tasks.pop(rid, None)

        task.add_done_callback(_cleanup)

    def register_background_task(self, run_id: str, task: asyncio.Task) -> None:
        """Public wrapper for tracking a background task tied to a run."""
        self._register_background_task(run_id, task)

    def _update_run_summary(self, run: AgentRun, **changes: Any) -> AgentRun:
        run.summary_json = {
            **(run.summary_json or {}),
            **changes,
        }
        run.updated_at = utc_now_iso()
        self.store.save_run(run)
        return run

    def _persist_diagnosis_contract(
        self,
        run: AgentRun,
        *,
        contract: Dict[str, Any],
        missing_fields: Optional[List[str]] = None,
        reask_count: Optional[int] = None,
        reask_max_rounds: Optional[int] = None,
        last_error: str = "",
    ) -> AgentRun:
        safe_contract = _normalize_diagnosis_contract(contract)
        summary = dict(run.summary_json or {})
        summary["diagnosis_contract"] = safe_contract
        summary["diagnosis_contract_missing_fields"] = (
            [item for item in (missing_fields or []) if _as_str(item).strip()]
            if missing_fields is not None
            else _diagnosis_contract_missing_fields(safe_contract)
        )
        if reask_count is not None:
            summary["diagnosis_contract_reask_count"] = max(0, int(reask_count))
        if reask_max_rounds is not None:
            summary["diagnosis_contract_reask_max_rounds"] = max(0, int(reask_max_rounds))
        if last_error:
            summary["diagnosis_contract_last_error"] = _as_str(last_error)
        else:
            summary.pop("diagnosis_contract_last_error", None)
        summary["diagnosis_contract_last_validated_at"] = utc_now_iso()
        context = dict(run.context_json or {})
        context["diagnosis_contract"] = safe_contract
        run.summary_json = summary
        run.context_json = context
        run.updated_at = utc_now_iso()
        self.store.save_run(run)
        return run

    def _merge_diagnosis_contract_sources(
        self,
        run: AgentRun,
        *,
        explicit_contract: Any = None,
        command_spec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        context = run.context_json if isinstance(run.context_json, dict) else {}
        pending = summary.get("pending_command_request") if isinstance(summary.get("pending_command_request"), dict) else {}
        sources: List[Any] = [
            explicit_contract,
            command_spec.get("diagnosis_contract") if isinstance(command_spec, dict) else None,
            pending.get("diagnosis_contract"),
            summary.get("diagnosis_contract"),
            context.get("diagnosis_contract"),
        ]
        merged: Dict[str, Any] = {}
        for source in sources:
            candidate = _normalize_diagnosis_contract(source)
            fault_summary = _as_str(candidate.get("fault_summary")).strip()
            if fault_summary and not _as_str(merged.get("fault_summary")).strip():
                merged["fault_summary"] = fault_summary
            evidence_gaps = _as_list(candidate.get("evidence_gaps"))
            if evidence_gaps and not _as_list(merged.get("evidence_gaps")):
                merged["evidence_gaps"] = evidence_gaps
            execution_plan = _as_list(candidate.get("execution_plan"))
            if execution_plan and not _as_list(merged.get("execution_plan")):
                merged["execution_plan"] = execution_plan
            why_command_needed = _as_str(candidate.get("why_command_needed")).strip()
            if why_command_needed and not _as_str(merged.get("why_command_needed")).strip():
                merged["why_command_needed"] = why_command_needed
        return _normalize_diagnosis_contract(merged)

    def _block_run_for_diagnosis_contract_incomplete(
        self,
        run: AgentRun,
        *,
        tool_call_id: str,
        tool_name: str,
        title: str,
        command: str,
        purpose: str,
        missing_fields: List[str],
        attempts: int,
        max_rounds: int,
    ) -> AgentRun:
        now_iso = utc_now_iso()
        previous_status = run.status
        summary = dict(run.summary_json or {})
        summary.pop("pending_action", None)
        summary.pop("pending_user_input", None)
        summary.pop("pending_command_request", None)
        run.status = RUN_STATUS_BLOCKED
        run.error_code = "diagnosis_contract_incomplete"
        run.error_detail = (
            "diagnosis_contract incomplete after re-ask limit: "
            f"missing={','.join(missing_fields)} attempts={int(attempts)} max_rounds={int(max_rounds)}"
        )
        run.updated_at = now_iso
        run.ended_at = now_iso
        run.summary_json = {
            **summary,
            "current_phase": "blocked",
            "blocked_reason": "diagnosis_contract_incomplete",
            "diagnosis_contract_missing_fields": [item for item in missing_fields if _as_str(item).strip()],
            "diagnosis_contract_reask_count": int(attempts),
            "diagnosis_contract_reask_max_rounds": int(max_rounds),
            "pending_approval_count": 0,
        }
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {
                "status": run.status,
                "previous_status": previous_status,
                "current_phase": "blocked",
                "blocked_reason": "diagnosis_contract_incomplete",
            },
        )
        self.append_event(
            run.run_id,
            event_protocol.TOOL_CALL_FINISHED,
            {
                "tool_call_id": _as_str(tool_call_id),
                "tool_name": _as_str(tool_name, "command.exec"),
                "title": _as_str(title, "执行命令"),
                "status": "failed",
                "command": _as_str(command),
                "purpose": _as_str(purpose),
                "message": (
                    "写命令被阻断：diagnosis_contract 缺少必填字段且超过重试上限，"
                    f"缺失字段：{_diagnosis_contract_missing_fields_text(missing_fields)}"
                ),
                "error_code": "diagnosis_contract_incomplete",
                "missing_fields": [item for item in missing_fields if _as_str(item).strip()],
            },
        )
        return run

    def _normalize_runtime_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        safe_options = dict(runtime_options) if isinstance(runtime_options, dict) else {}
        timeout_default = _as_int(os.getenv("AI_RUNTIME_APPROVAL_TIMEOUT_SECONDS"), 900)
        timeout_raw = safe_options.get(
            "approval_timeout_seconds",
            safe_options.get("approvalTimeoutSeconds", timeout_default),
        )
        timeout_seconds = max(1, min(7200, _as_int(timeout_raw, timeout_default)))
        reject_strategy_raw = _as_str(
            safe_options.get("approval_reject_strategy")
            or safe_options.get("on_approval_rejected")
            or "replan"
        ).strip().lower()
        reject_strategy = reject_strategy_raw if reject_strategy_raw in {"replan", "terminate"} else "replan"
        replan_max_raw = safe_options.get(
            "approval_replan_max_rounds",
            safe_options.get("replan_max_rounds", 1),
        )
        replan_max_rounds = max(0, min(10, _as_int(replan_max_raw, 1)))
        unknown_retry_raw = safe_options.get(
            "unknown_semantics_max_retries",
            safe_options.get("unknown_command_max_retries", 1),
        )
        unknown_semantics_max_retries = max(0, min(10, _as_int(unknown_retry_raw, 1)))
        diagnosis_reask_default = _as_int(
            os.getenv("AI_RUNTIME_DIAGNOSIS_CONTRACT_REASK_MAX_ROUNDS"),
            1,
        )
        diagnosis_reask_raw = safe_options.get(
            "diagnosis_contract_reask_max_rounds",
            safe_options.get("diagnosis_contract_max_reasks", diagnosis_reask_default),
        )
        diagnosis_contract_reask_max_rounds = max(0, min(10, _as_int(diagnosis_reask_raw, diagnosis_reask_default)))
        recovery_rounds_raw = safe_options.get(
            "command_recovery_max_rounds",
            safe_options.get("commandRecoveryMaxRounds", 2),
        )
        command_recovery_max_rounds = max(1, min(4, _as_int(recovery_rounds_raw, 2)))
        timeout_recovery_rounds_raw = safe_options.get(
            "timeout_recovery_max_rounds",
            safe_options.get("timeoutRecoveryMaxRounds", 2),
        )
        timeout_recovery_max_rounds = max(1, min(4, _as_int(timeout_recovery_rounds_raw, 2)))
        return {
            **safe_options,
            "approval_timeout_seconds": timeout_seconds,
            "approval_reject_strategy": reject_strategy,
            "approval_replan_max_rounds": replan_max_rounds,
            "unknown_semantics_max_retries": unknown_semantics_max_retries,
            "diagnosis_contract_reask_max_rounds": diagnosis_contract_reask_max_rounds,
            "command_recovery_max_rounds": command_recovery_max_rounds,
            "timeout_recovery_max_rounds": timeout_recovery_max_rounds,
        }

    def _runtime_options(self, run: AgentRun) -> Dict[str, Any]:
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        return self._normalize_runtime_options(summary.get("runtime_options"))

    def _pending_action(self, run: AgentRun) -> Dict[str, Any]:
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        pending = summary.get("pending_action")
        return dict(pending) if isinstance(pending, dict) else {}

    def _has_unresolved_pending_action(self, run: Optional[AgentRun]) -> bool:
        if run is None:
            return False
        if run.status in {RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_WAITING_USER_INPUT}:
            return True
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        pending = summary.get("pending_action")
        if isinstance(pending, dict):
            pending_status = _as_str(pending.get("status"), "pending").strip().lower()
            if pending_status == "pending":
                return True
        if _as_int(summary.get("pending_approval_count"), 0) > 0:
            return True
        if isinstance(summary.get("pending_user_input"), dict):
            return True
        return False

    def _ensure_no_pending_action(self, run: AgentRun, *, allow_kind: str = "") -> None:
        pending = self._pending_action(run)
        if not pending:
            return
        if _as_str(pending.get("status"), "pending").strip().lower() != "pending":
            return
        pending_kind = _as_str(pending.get("kind")).strip().lower()
        if allow_kind and pending_kind == _as_str(allow_kind).strip().lower():
            return
        raise RuntimeError("run has unresolved pending action")

    def _cancel_pending_action_timer(self, run_id: str) -> None:
        safe_run_id = _as_str(run_id)
        with self._lock:
            timer = self._pending_action_timers.pop(safe_run_id, None)
        if timer is not None and not timer.done():
            timer.cancel()

    def _set_pending_action_timer(self, run_id: str, task: asyncio.Task) -> None:
        safe_run_id = _as_str(run_id)
        with self._lock:
            existing = self._pending_action_timers.get(safe_run_id)
            if existing is not None and not existing.done():
                existing.cancel()
            self._pending_action_timers[safe_run_id] = task

        def _cleanup(done_task: asyncio.Task, *, rid: str = safe_run_id) -> None:
            with self._lock:
                current = self._pending_action_timers.get(rid)
                if current is done_task:
                    self._pending_action_timers.pop(rid, None)

        task.add_done_callback(_cleanup)

    def _schedule_approval_timeout(
        self,
        *,
        run_id: str,
        approval_id: str,
        timeout_seconds: int,
    ) -> None:
        if timeout_seconds <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _watch() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
            except asyncio.CancelledError:
                return
            self._handle_approval_timeout(
                run_id,
                approval_id=approval_id,
                timeout_seconds=timeout_seconds,
            )

        task = loop.create_task(_watch())
        self._set_pending_action_timer(run_id, task)

    def _handle_approval_timeout(
        self,
        run_id: str,
        *,
        approval_id: str,
        timeout_seconds: int,
    ) -> None:
        run = self.get_run(run_id)
        if run is None or is_terminal_run_status(run.status) or run.status != RUN_STATUS_WAITING_APPROVAL:
            return
        summary = dict(run.summary_json or {})
        pending_approval = summary.get("pending_approval") if isinstance(summary.get("pending_approval"), dict) else {}
        if _as_str(pending_approval.get("approval_id")) != _as_str(approval_id):
            return
        now_iso = utc_now_iso()
        previous_status = run.status
        pending_command_request = summary.pop("pending_command_request", None)
        summary.pop("pending_approval", None)
        summary.pop("pending_action", None)
        run.status = RUN_STATUS_BLOCKED
        run.error_code = "approval_timeout"
        run.error_detail = f"approval timeout after {int(timeout_seconds)}s"
        run.updated_at = now_iso
        run.ended_at = now_iso
        run.summary_json = {
            **summary,
            "current_phase": "blocked",
            "pending_approval_count": 0,
            "blocked_reason": "approval_timeout",
            "last_approval_timeout": {
                "approval_id": _as_str(approval_id),
                "timeout_seconds": int(timeout_seconds),
                "timed_out_at": now_iso,
            },
        }
        self.store.save_run(run)
        timeout_payload = {
            "approval_id": _as_str(approval_id),
            "timeout_seconds": int(timeout_seconds),
            "timed_out_at": now_iso,
        }
        self.append_event(run.run_id, event_protocol.APPROVAL_TIMEOUT, timeout_payload)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {
                "status": run.status,
                "previous_status": previous_status,
                "current_phase": "blocked",
                "blocked_reason": "approval_timeout",
            },
        )
        if isinstance(pending_command_request, dict):
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_FINISHED,
                {
                    "tool_call_id": _as_str(pending_command_request.get("tool_call_id")),
                    "tool_name": _as_str(pending_command_request.get("tool_name"), "command.exec"),
                    "title": _as_str(pending_command_request.get("title"), "执行命令"),
                    "status": "timed_out",
                    "command": _as_str(pending_command_request.get("command")),
                    "purpose": _as_str(pending_command_request.get("purpose")),
                    "message": "approval timed out",
                },
            )

    def _append_approval_context(self, run: AgentRun, approval_payload: Dict[str, Any]) -> None:
        summary = dict(run.summary_json or {})
        history = summary.get("approval_history") if isinstance(summary.get("approval_history"), list) else []
        history.append(dict(approval_payload))
        summary["approval_history"] = history[-20:]
        context_json = dict(run.context_json or {})
        feedback = context_json.get("approval_feedback") if isinstance(context_json.get("approval_feedback"), list) else []
        feedback.append(
            {
                "approval_id": _as_str(approval_payload.get("approval_id")),
                "decision": _as_str(approval_payload.get("decision")),
                "comment": _as_str(approval_payload.get("comment")),
                "resolved_at": _as_str(approval_payload.get("resolved_at")),
            }
        )
        context_json["approval_feedback"] = feedback[-20:]
        run.summary_json = summary
        run.context_json = context_json

    def append_assistant_delta(
        self,
        run_id: str,
        *,
        text: str,
        assistant_message_id: str = "",
    ) -> Optional[RunEvent]:
        run = self.get_run(run_id)
        if run is None:
            return None
        safe_text = _as_str(text)
        if not safe_text:
            return None
        return self.append_event(
            run.run_id,
            event_protocol.ASSISTANT_DELTA,
            {
                "assistant_message_id": _as_str(assistant_message_id) or run.assistant_message_id,
                "text": safe_text,
            },
        )

    def finalize_assistant_message(
        self,
        run_id: str,
        *,
        content: str,
        assistant_message_id: str = "",
        references: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[RunEvent]:
        run = self.get_run_fresh(run_id)
        if run is None:
            return None
        if self._has_unresolved_pending_action(run):
            return None
        payload: Dict[str, Any] = {
            "assistant_message_id": _as_str(assistant_message_id) or run.assistant_message_id,
            "content": _as_str(content),
        }
        if isinstance(references, list):
            payload["references"] = references
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = metadata
        return self.append_event(
            run.run_id,
            event_protocol.ASSISTANT_MESSAGE_FINALIZED,
            payload,
        )

    def finish_run(
        self,
        run_id: str,
        *,
        summary_updates: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        final_status: str = "completed",
    ) -> Optional[AgentRun]:
        run = self.get_run_fresh(run_id)
        if run is None or is_terminal_run_status(run.status):
            return run
        if self._has_unresolved_pending_action(run):
            return run
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        self._cancel_pending_action_timer(run_id)
        now_iso = utc_now_iso()
        previous_status = run.status
        normalized_final_status = _as_str(final_status).strip().lower()
        if normalized_final_status not in {"completed", "blocked"}:
            normalized_final_status = "completed"
        if normalized_final_status == "blocked":
            run.status = RUN_STATUS_BLOCKED
            current_phase = "blocked"
        else:
            run.status = RUN_STATUS_COMPLETED
            current_phase = "completed"
        run.updated_at = now_iso
        run.ended_at = now_iso
        next_summary = {
            **(run.summary_json or {}),
            **(summary_updates if isinstance(summary_updates, dict) else {}),
            "current_phase": current_phase,
        }
        if current_phase != "blocked":
            next_summary.pop("blocked_reason", None)
        run.summary_json = next_summary
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status},
        )
        self.append_event(
            run.run_id,
            event_protocol.RUN_FINISHED,
            payload if isinstance(payload, dict) else {"status": run.status},
        )
        # Phase 4: auto-save remediation plan to knowledge base when run completes
        if normalized_final_status == "completed":
            self._try_auto_save_remediation_plan(run)
        return run

    def _try_auto_save_remediation_plan(self, run: "AgentRun") -> None:
        """
        Phase 4: After a diagnostic run completes, extract a RemediationPlan
        from the run's evidence/summary and save it to the knowledge base as
        a pending-verification case.

        This is a best-effort fire-and-forget operation — any failure is logged
        and swallowed so it never affects the run's completion status.
        """
        try:
            from ai.agent_runtime.auto_remediation import build_and_save_remediation_plan
            build_and_save_remediation_plan(run=run, service=self)
        except ImportError:
            pass  # auto_remediation module not yet available
        except Exception as exc:
            logger.warning(
                "Phase4 KB auto-save failed for run_id=%s: %s",
                run.run_id,
                exc,
                exc_info=False,
            )

    def fail_run(
        self,
        run_id: str,
        *,
        error_code: str = "runtime_failed",
        error_detail: str = "",
        summary_updates: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[AgentRun]:
        run = self.get_run(run_id)
        if run is None or is_terminal_run_status(run.status):
            return run
        self._cancel_pending_action_timer(run_id)
        now_iso = utc_now_iso()
        previous_status = run.status
        run.status = RUN_STATUS_FAILED
        run.error_code = _as_str(error_code, "runtime_failed")
        run.error_detail = _as_str(error_detail)
        run.updated_at = now_iso
        run.ended_at = now_iso
        run.summary_json = {
            **(run.summary_json or {}),
            **(summary_updates if isinstance(summary_updates, dict) else {}),
            "current_phase": "failed",
        }
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status},
        )
        self.append_event(
            run.run_id,
            event_protocol.RUN_FAILED,
            {
                "status": run.status,
                "error_code": run.error_code,
                "error_detail": run.error_detail,
                **(payload if isinstance(payload, dict) else {}),
            },
        )
        return run

    def create_run(
        self,
        *,
        session_id: str,
        question: str,
        analysis_context: Optional[Dict[str, Any]] = None,
        runtime_options: Optional[Dict[str, Any]] = None,
    ) -> AgentRun:
        safe_context = analysis_context if isinstance(analysis_context, dict) else {}
        analysis_type = _as_str(safe_context.get("analysis_type"), "log").strip().lower() or "log"
        trace_id = _as_str(safe_context.get("trace_id")).strip()
        if analysis_type == "trace" and not trace_id:
            safe_context = dict(safe_context)
            safe_context["analysis_type"] = "log"
            safe_context["analysis_type_original"] = "trace"
            safe_context["analysis_type_downgraded"] = True
            safe_context["analysis_type_downgrade_reason"] = "trace_id_missing"
            analysis_type = "log"
        else:
            safe_context = dict(safe_context)
            safe_context["analysis_type"] = analysis_type
        safe_runtime_options = self._normalize_runtime_options(runtime_options)
        safe_conversation_id = _as_str(safe_runtime_options.get("conversation_id")) or build_id("conv")
        safe_runtime_options["conversation_id"] = safe_conversation_id
        safe_question = _as_str(question).strip()
        if not safe_question:
            raise ValueError("question is required")

        created_at = utc_now_iso()
        summary_json = {
            "title": safe_question[:120],
            "current_phase": "planning",
            "iteration": 0,
            "runtime_options": safe_runtime_options,
            "pending_approval_count": 0,
            "replan_count": 0,
        }
        if safe_context.get("analysis_type_downgraded"):
            summary_json.update(
                {
                    "analysis_type_downgraded": True,
                    "analysis_type_original": _as_str(safe_context.get("analysis_type_original")),
                    "analysis_type_downgrade_reason": _as_str(safe_context.get("analysis_type_downgrade_reason")),
                }
            )
        run = AgentRun(
            run_id=build_id("run"),
            session_id=_as_str(session_id) or build_id("sess"),
            conversation_id=safe_conversation_id,
            analysis_type=analysis_type,
            engine="agent-runtime-v1",
            runtime_version="v1",
            user_message_id=build_id("msg-u"),
            assistant_message_id=build_id("msg"),
            service_name=_as_str(safe_context.get("service_name")),
            trace_id=_as_str(safe_context.get("trace_id")),
            status=RUN_STATUS_RUNNING,
            question=safe_question,
            input_json={"question": safe_question},
            context_json=safe_context,
            summary_json=summary_json,
            created_at=created_at,
            updated_at=created_at,
        )
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STARTED,
            {"status": run.status, "analysis_type": run.analysis_type, "engine": run.engine},
        )
        self.append_event(
            run.run_id,
            event_protocol.MESSAGE_STARTED,
            {"assistant_message_id": run.assistant_message_id},
        )
        self.append_event(
            run.run_id,
            event_protocol.REASONING_STEP,
            {
                "step_id": "step-0001",
                "phase": "planning",
                "title": "初始化运行上下文",
                "status": "in_progress",
                "iteration": 0,
            },
        )
        self.append_event(
            run.run_id,
            event_protocol.REASONING_SUMMARY_DELTA,
            {
                "step_id": "step-0001",
                "phase": "planning",
                "text": "运行已创建，已完成基础上下文初始化。",
            },
        )

        # ai-runtime-lab: emit skill selection events when lab profile is active
        if _as_str(safe_context.get("runtime_profile")) == _AI_RUNTIME_LAB_PROFILE:
            self._emit_skill_selection_events(run, safe_context)

        return run

    def _emit_skill_selection_events(
        self,
        run: AgentRun,
        context: Dict[str, Any],
    ) -> None:
        """
        Emit skill_matched and skill_step_planned events for the ai-runtime-lab profile.

        This runs synchronously in create_run() so the client sees skill selection
        before the first command is dispatched.
        """
        try:
            from ai.skills.base import SkillContext
            from ai.skills.matcher import (
                extract_auto_selected_skills,
                get_skill_selection_summary,
            )

            skill_ctx = SkillContext.from_dict({
                **context,
                "question": run.question,
            })
            matched_skills = extract_auto_selected_skills(skill_ctx, threshold=0.35, max_skills=3)

            if not matched_skills:
                self.append_event(
                    run.run_id,
                    event_protocol.REASONING_STEP,
                    {
                        "step_id": "skill-select-0",
                        "phase": "planning",
                        "title": "技能选择",
                        "status": "info",
                        "iteration": 0,
                    },
                )
                self.append_event(
                    run.run_id,
                    event_protocol.REASONING_SUMMARY_DELTA,
                    {
                        "step_id": "skill-select-0",
                        "phase": "planning",
                        "text": "当前上下文未匹配到专项诊断技能，将使用通用分析流程。",
                    },
                )
                return

            skill_names = [s.name for s in matched_skills]
            summary = get_skill_selection_summary(skill_ctx, skill_names)

            # Emit skill_matched event
            self.append_event(
                run.run_id,
                event_protocol.SKILL_MATCHED,
                {
                    "selected_skills": [
                        {
                            "name": s.name,
                            "display_name": s.display_name,
                            "description": s.description,
                            "risk_level": s.risk_level,
                        }
                        for s in matched_skills
                    ],
                    "summary": summary,
                },
            )

            # Emit reasoning step for the selection
            self.append_event(
                run.run_id,
                event_protocol.REASONING_STEP,
                {
                    "step_id": "skill-select-0",
                    "phase": "planning",
                    "title": f"已选择 {len(matched_skills)} 个诊断技能",
                    "status": "success",
                    "iteration": 0,
                },
            )
            self.append_event(
                run.run_id,
                event_protocol.REASONING_SUMMARY_DELTA,
                {
                    "step_id": "skill-select-0",
                    "phase": "planning",
                    "text": summary,
                },
            )

            # Emit step planned events for each skill's steps
            step_seq = 1
            for skill in matched_skills:
                try:
                    steps = skill.plan_steps(skill_ctx)
                except Exception:
                    continue
                for step in steps:
                    self.append_event(
                        run.run_id,
                        event_protocol.SKILL_STEP_PLANNED,
                        {
                            "skill_name": skill.name,
                            "skill_display_name": skill.display_name,
                            "step_id": step.step_id,
                            "title": step.title,
                            "purpose": step.purpose,
                            "seq": step_seq,
                        },
                    )
                    step_seq += 1

            # Persist selected skills in run summary
            summary_json = dict(run.summary_json or {})
            summary_json["selected_skills"] = skill_names
            summary_json["skill_selection_policy"] = "pattern_hits_required+threshold_0_35"
            summary_json["skill_step_count"] = step_seq - 1
            run.summary_json = summary_json
            run.updated_at = utc_now_iso()
            self.store.save_run(run)

        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to emit skill selection events for run_id=%s", run.run_id
            )

    def get_run(self, run_id: str, *, fresh: bool = False) -> Optional[AgentRun]:
        return self.store.get_run(run_id, fresh=fresh)

    def get_run_fresh(self, run_id: str) -> Optional[AgentRun]:
        return self.store.get_run(run_id, fresh=True)

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 500) -> List[RunEvent]:
        return self.store.list_events(run_id, after_seq=after_seq, limit=limit)

    def request_approval(
        self,
        run_id: str,
        *,
        approval_id: str = "",
        title: str = "",
        reason: str = "",
        command: str = "",
        purpose: str = "",
        command_type: str = "unknown",
        risk_level: str = "high",
        command_family: str = "unknown",
        approval_policy: str = "elevation_required",
        executor_type: str = "local_process",
        executor_profile: str = "local-default",
        target_kind: str = "runtime_node",
        target_identity: str = "runtime:local",
        requires_confirmation: bool = True,
        requires_elevation: bool = False,
    ) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            raise RuntimeError("run is already terminal")
        self._ensure_no_pending_action(run)

        now_iso = utc_now_iso()
        previous_status = run.status
        safe_approval_id = _as_str(approval_id) or build_id("apr")
        runtime_options = self._runtime_options(run)
        timeout_seconds = max(1, _as_int(runtime_options.get("approval_timeout_seconds"), 900))
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
        ).isoformat().replace("+00:00", "Z")
        approval_payload = {
            "approval_id": safe_approval_id,
            "title": _as_str(title) or "需要用户确认后继续执行",
            "reason": _as_str(reason) or "runtime requested approval",
            "command": _as_str(command),
            "purpose": _as_str(purpose),
            "command_type": _as_str(command_type, "unknown"),
            "risk_level": _as_str(risk_level, "high"),
            "command_family": _as_str(command_family, "unknown"),
            "approval_policy": _as_str(approval_policy, "elevation_required"),
            "executor_type": _as_str(executor_type, "local_process"),
            "executor_profile": _as_str(executor_profile, "local-default"),
            "target_kind": _as_str(target_kind, "runtime_node"),
            "target_identity": _as_str(target_identity, "runtime:local"),
            "requires_confirmation": bool(requires_confirmation),
            "requires_elevation": bool(requires_elevation),
            "requested_at": now_iso,
            "expires_at": expires_at,
            "timeout_seconds": timeout_seconds,
        }

        pending_action = {
            "kind": "approval",
            "id": safe_approval_id,
            "status": "pending",
            "created_at": now_iso,
            "expires_at": expires_at,
        }
        run.status = RUN_STATUS_WAITING_APPROVAL
        run.updated_at = now_iso
        run.summary_json = {
            **(run.summary_json or {}),
            "current_phase": "waiting_approval",
            "pending_approval_count": 1,
            "pending_approval": approval_payload,
            "pending_action": pending_action,
        }
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status},
        )
        self.append_event(run.run_id, event_protocol.APPROVAL_REQUIRED, approval_payload)
        self.append_event(
            run.run_id,
            event_protocol.ACTION_WAITING_APPROVAL,
            {
                "approval_id": safe_approval_id,
                "title": approval_payload["title"],
                "command": approval_payload["command"],
                "purpose": approval_payload["purpose"],
                "timeout_seconds": timeout_seconds,
                "expires_at": expires_at,
            },
        )
        self._schedule_approval_timeout(
            run_id=run.run_id,
            approval_id=safe_approval_id,
            timeout_seconds=timeout_seconds,
        )
        return {"run": run, "approval": approval_payload}

    def resolve_approval(
        self,
        run_id: str,
        *,
        approval_id: str,
        decision: str,
        comment: str = "",
        confirmed: bool = True,
        elevated: bool = False,
    ) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            raise RuntimeError("run is already terminal")
        normalized_decision = _as_str(decision).strip().lower()
        if normalized_decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        requested_approval_id = _as_str(approval_id)
        if run.status != RUN_STATUS_WAITING_APPROVAL:
            summary = dict(run.summary_json or {})
            last_approval = summary.get("last_approval") if isinstance(summary.get("last_approval"), dict) else {}
            last_approval_id = _as_str(last_approval.get("approval_id"))
            last_decision = _as_str(last_approval.get("decision")).strip().lower()
            if (
                requested_approval_id
                and last_approval_id
                and requested_approval_id == last_approval_id
                and last_decision in {"approved", "rejected"}
            ):
                approval_payload = {
                    "approval_id": last_approval_id,
                    "decision": last_decision,
                    "comment": _as_str(last_approval.get("comment")),
                    "confirmed": bool(last_approval.get("confirmed")),
                    "elevated": bool(last_approval.get("elevated")),
                    "resolved_at": _as_str(last_approval.get("resolved_at")),
                }
                replan_payload = summary.get("last_replan") if isinstance(summary.get("last_replan"), dict) else None
                return {
                    "run": run,
                    "approval": approval_payload,
                    "resume_command_request": None,
                    "replan": replan_payload if last_decision == "rejected" else None,
                    "idempotent": True,
                }
            raise RuntimeError("run is not waiting approval")
        self._cancel_pending_action_timer(run_id)

        now_iso = utc_now_iso()
        previous_status = run.status
        previous_summary = dict(run.summary_json or {})
        pending_approval = (
            previous_summary.get("pending_approval")
            if isinstance(previous_summary.get("pending_approval"), dict)
            else {}
        )
        pending_approval_id = _as_str(pending_approval.get("approval_id"))
        if requested_approval_id and pending_approval_id and requested_approval_id != pending_approval_id:
            raise ValueError("approval_id does not match pending approval")
        pending_command_request = previous_summary.pop("pending_command_request", None)
        previous_summary.pop("pending_action", None)
        previous_summary.pop("pending_approval", None)
        approval_payload = {
            "approval_id": requested_approval_id or pending_approval_id or build_id("apr"),
            "decision": normalized_decision,
            "comment": _as_str(comment),
            "confirmed": bool(confirmed),
            "elevated": bool(elevated),
            "resolved_at": now_iso,
        }
        runtime_options = self._runtime_options(run)
        reject_strategy = _as_str(runtime_options.get("approval_reject_strategy"), "replan").strip().lower()
        if reject_strategy not in {"replan", "terminate"}:
            reject_strategy = "replan"
        replan_max_rounds = max(0, _as_int(runtime_options.get("approval_replan_max_rounds"), 1))
        current_replan_count = max(0, _as_int(previous_summary.get("replan_count"), 0))

        next_phase = "acting"
        action_payload: Dict[str, Any] = {
            "approval_id": approval_payload["approval_id"],
            "decision": normalized_decision,
            "comment": approval_payload["comment"],
        }
        if normalized_decision == "approved":
            run.status = RUN_STATUS_RUNNING
            run.updated_at = now_iso
            run.summary_json = {
                **previous_summary,
                "current_phase": "acting",
                "pending_approval_count": 0,
                "last_approval": approval_payload,
            }
            run.summary_json.pop("blocked_reason", None)
            action_payload["outcome"] = "resumed"
        else:
            should_replan = reject_strategy == "replan" and current_replan_count < replan_max_rounds
            if should_replan:
                run.status = RUN_STATUS_RUNNING
                run.updated_at = now_iso
                run.ended_at = None
                next_phase = "planning"
                run.summary_json = {
                    **previous_summary,
                    "current_phase": next_phase,
                    "pending_approval_count": 0,
                    "last_approval": approval_payload,
                    "replan_count": current_replan_count + 1,
                    "last_replan": {
                        "reason": "approval_rejected",
                        "comment": _as_str(comment),
                        "replanned_at": now_iso,
                    },
                }
                run.summary_json.pop("blocked_reason", None)
                action_payload.update(
                    {
                        "strategy": "replan",
                        "outcome": "replanned",
                        "replan_count": current_replan_count + 1,
                        "replan_max_rounds": replan_max_rounds,
                    }
                )
            else:
                run.status = RUN_STATUS_BLOCKED
                run.updated_at = now_iso
                run.ended_at = now_iso
                next_phase = "blocked"
                blocked_reason = "approval_rejected" if reject_strategy == "terminate" else "approval_rejected_replan_limit"
                run.summary_json = {
                    **previous_summary,
                    "current_phase": next_phase,
                    "pending_approval_count": 0,
                    "last_approval": approval_payload,
                    "blocked_reason": blocked_reason,
                    "replan_count": current_replan_count,
                }
                action_payload.update(
                    {
                        "strategy": reject_strategy,
                        "outcome": "terminated",
                        "blocked_reason": blocked_reason,
                        "replan_count": current_replan_count,
                        "replan_max_rounds": replan_max_rounds,
                    }
                )

        self._append_approval_context(run, approval_payload)
        self.store.save_run(run)
        self.append_event(run.run_id, event_protocol.APPROVAL_RESOLVED, approval_payload)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {
                "status": run.status,
                "previous_status": previous_status,
                "current_phase": next_phase,
            },
        )
        if normalized_decision == "approved":
            self.append_event(run.run_id, event_protocol.ACTION_RESUMED, action_payload)
        else:
            self.append_event(run.run_id, event_protocol.ACTION_REPLANNED, action_payload)

        if normalized_decision == "rejected" and isinstance(pending_command_request, dict):
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_FINISHED,
                {
                    "tool_call_id": _as_str(pending_command_request.get("tool_call_id")),
                    "tool_name": _as_str(pending_command_request.get("tool_name"), "command.exec"),
                    "title": _as_str(pending_command_request.get("title"), "执行命令"),
                    "status": "rejected",
                    "command": _as_str(pending_command_request.get("command")),
                    "purpose": _as_str(pending_command_request.get("purpose")),
                    "message": _as_str(comment, "approval rejected"),
                },
            )
        return {
            "run": run,
            "approval": approval_payload,
            "resume_command_request": pending_command_request if normalized_decision == "approved" else None,
            "replan": action_payload if normalized_decision == "rejected" else None,
        }

    def request_user_input(
        self,
        run_id: str,
        *,
        title: str = "",
        prompt: str = "",
        reason: str = "",
        action_id: str = "",
        command: str = "",
        purpose: str = "",
        kind: str = "",
        question_kind: str = "",
        recovery_attempts: int = 0,
        source_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            raise RuntimeError("run is already terminal")
        self._ensure_no_pending_action(run)

        now_iso = utc_now_iso()
        previous_status = run.status
        safe_action_id = _as_str(action_id) or build_id("input")
        safe_kind = _as_str(kind).strip().lower() or "user_input"
        request_payload = {
            "action_id": safe_action_id,
            "title": _as_str(title) or "还需要你确认一个关键信息",
            "prompt": _as_str(prompt) or "我还缺少一个关键信息后继续排查，请直接告诉我你希望我先确认什么。",
            "reason": _as_str(reason),
            "command": _as_str(command),
            "purpose": _as_str(purpose),
            "kind": safe_kind,
            "question_kind": _as_str(question_kind).strip(),
            "recovery_attempts": max(0, _as_int(recovery_attempts, 0)),
            "requested_at": now_iso,
        }
        if isinstance(source_context, dict) and source_context:
            request_payload["source_context"] = dict(source_context)
        run.status = RUN_STATUS_WAITING_USER_INPUT
        run.updated_at = now_iso
        run.summary_json = {
            **(run.summary_json or {}),
            "current_phase": "waiting_user_input",
            "pending_user_input": request_payload,
            "pending_action": {
                "kind": "user_input",
                "id": safe_action_id,
                "status": "pending",
                "created_at": now_iso,
                "question_kind": _as_str(question_kind).strip(),
            },
        }
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status, "current_phase": "waiting_user_input"},
        )
        self.append_event(run.run_id, event_protocol.ACTION_WAITING_USER_INPUT, request_payload)
        return {"run": run, "user_input_request": request_payload}

    def submit_user_input(
        self,
        run_id: str,
        *,
        text: str,
        source: str = "user",
    ) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            raise RuntimeError("run is already terminal")
        if run.status != RUN_STATUS_WAITING_USER_INPUT:
            raise RuntimeError("run is not waiting user input")

        safe_text = _as_str(text).strip()
        if not safe_text:
            raise ValueError("text is required")

        now_iso = utc_now_iso()
        previous_status = run.status
        previous_summary = dict(run.summary_json or {})
        pending_request = (
            previous_summary.get("pending_user_input")
            if isinstance(previous_summary.get("pending_user_input"), dict)
            else {}
        )
        history = previous_summary.get("user_input_history") if isinstance(previous_summary.get("user_input_history"), list) else []
        user_input_payload = {
            "text": safe_text,
            "business_answer_text": safe_text,
            "source": _as_str(source, "user"),
            "submitted_at": now_iso,
            "action_id": _as_str(pending_request.get("action_id")),
            "question_kind": _as_str(pending_request.get("question_kind")).strip(),
        }
        if isinstance(pending_request.get("source_context"), dict):
            user_input_payload["source_context"] = dict(pending_request.get("source_context"))
        history.append(user_input_payload)
        pending_command_request = (
            previous_summary.get("pending_command_request")
            if isinstance(previous_summary.get("pending_command_request"), dict)
            else {}
        )
        if isinstance(pending_command_request, dict) and pending_command_request:
            existing_contract = self._merge_diagnosis_contract_sources(
                run,
                explicit_contract=pending_command_request.get("diagnosis_contract"),
                command_spec=pending_command_request.get("command_spec")
                if isinstance(pending_command_request.get("command_spec"), dict)
                else None,
            )
            parsed_contract = _parse_diagnosis_contract_from_user_text(safe_text)
            if parsed_contract:
                merged_contract = _normalize_diagnosis_contract(
                    {
                        **existing_contract,
                        **parsed_contract,
                    }
                )
            else:
                merged_contract = _normalize_diagnosis_contract(existing_contract)
                if not _as_str(merged_contract.get("why_command_needed")).strip():
                    merged_contract["why_command_needed"] = safe_text
                if not _as_str(merged_contract.get("fault_summary")).strip():
                    merged_contract["fault_summary"] = safe_text[:160]
            missing_fields = _diagnosis_contract_missing_fields(merged_contract)
            pending_command_request = {
                **pending_command_request,
                "diagnosis_contract": merged_contract,
                "diagnosis_contract_missing_fields": missing_fields,
            }
            previous_summary["pending_command_request"] = pending_command_request
            previous_summary["diagnosis_contract"] = merged_contract
            previous_summary["diagnosis_contract_missing_fields"] = missing_fields
            context = dict(run.context_json or {})
            context["diagnosis_contract"] = merged_contract
            run.context_json = context
        previous_summary.pop("pending_action", None)
        previous_summary.pop("pending_user_input", None)
        run.status = RUN_STATUS_RUNNING
        run.updated_at = now_iso
        run.summary_json = {
            **previous_summary,
            "current_phase": "planning",
            "last_user_input": user_input_payload,
            "user_input_history": history[-20:],
        }
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status, "current_phase": "planning"},
        )
        self.append_event(
            run.run_id,
            event_protocol.ACTION_RESUMED,
            {
                "kind": "user_input",
                "action_id": _as_str(user_input_payload.get("action_id")),
                "text": safe_text,
                "resumed_at": now_iso,
            },
        )
        return {"run": run, "user_input": user_input_payload}

    def _request_business_question(
        self,
        run: AgentRun,
        *,
        action_id: str,
        command: str,
        purpose: str,
        title: str,
        failure_code: str,
        failure_message: str,
        missing_fields: Optional[List[str]] = None,
        recovery_attempts: Optional[List[Dict[str, Any]]] = None,
        recovery_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        attempts = recovery_attempts if isinstance(recovery_attempts, list) else []
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        last_user_input = summary.get("last_user_input") if isinstance(summary.get("last_user_input"), dict) else {}
        user_input_history = (
            summary.get("user_input_history")
            if isinstance(summary.get("user_input_history"), list)
            else []
        )
        latest_history_input = (
            user_input_history[-1]
            if user_input_history and isinstance(user_input_history[-1], dict)
            else {}
        )
        last_user_input_question_kind = (
            _as_str(last_user_input.get("question_kind")).strip()
            or _as_str(summary.get("last_user_question_kind")).strip()
            or _as_str(latest_history_input.get("question_kind")).strip()
        )
        last_user_input_action_id = (
            _as_str(last_user_input.get("action_id")).strip()
            or _as_str(latest_history_input.get("action_id")).strip()
        )
        last_user_input_text = (
            _as_str(last_user_input.get("business_answer_text")).strip()
            or _as_str(last_user_input.get("text")).strip()
            or _as_str(latest_history_input.get("business_answer_text")).strip()
            or _as_str(latest_history_input.get("text")).strip()
        )
        question_payload = build_business_question(
            failure_code=failure_code,
            failure_message=failure_message,
            purpose=purpose,
            title=title,
            command=command,
            missing_fields=missing_fields or [],
            recovery_attempts=len(attempts),
            current_action_id=action_id,
            last_user_input_question_kind=last_user_input_question_kind,
            last_user_input_action_id=last_user_input_action_id,
            last_user_input_text=last_user_input_text,
        )
        self._update_run_summary(
            run,
            last_recovery_failure_code=_as_str(failure_code).strip(),
            last_recovery_attempts=len(attempts),
            last_recovery_attempt_history=attempts[-10:],
            last_user_question_kind=_as_str(question_payload.get("question_kind")).strip(),
        )
        return self.request_user_input(
            run.run_id,
            action_id=action_id,
            title=_as_str(question_payload.get("title")).strip(),
            prompt=_as_str(question_payload.get("prompt")).strip(),
            reason=_as_str(question_payload.get("reason")).strip(),
            command=command,
            purpose=purpose,
            kind="business_question",
            question_kind=_as_str(question_payload.get("question_kind")).strip(),
            recovery_attempts=len(attempts),
            source_context={
                "failure_code": _as_str(failure_code).strip(),
                "failure_message": _as_str(failure_message).strip(),
                "missing_fields": [item for item in (missing_fields or []) if _as_str(item).strip()],
                "recovery": (
                    dict(recovery_context)
                    if isinstance(recovery_context, dict) and recovery_context
                    else {}
                ),
            },
        )

    def _emit_pre_command_plan(
        self,
        run: AgentRun,
        *,
        tool_call_id: str,
        action_id: str,
        command: str,
        purpose: str,
        title: str,
        command_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        safe_command = _normalize_command_for_history(command)
        if not safe_command:
            return
        safe_purpose = _as_str(purpose).strip() or _as_str(title).strip() or "执行排障命令"
        safe_meta = command_meta if isinstance(command_meta, dict) else {}
        safe_reason = (
            _as_str(safe_meta.get("reason")).strip()
            or f"当前步骤“{_as_str(title).strip() or '执行命令'}”需要先获取命令输出。"
        )
        safe_expected = (
            _as_str(safe_purpose).strip()
            or "输出可用于判断下一步。"
        )
        summary = run.summary_json if isinstance(run.summary_json, dict) else {}
        iteration = max(0, _as_int(summary.get("iteration"), 0))
        self.append_event(
            run.run_id,
            event_protocol.REASONING_STEP,
            {
                "step_id": build_id("step"),
                "phase": "action",
                "title": "执行前计划",
                "status": "info",
                "detail": _build_command_plan_detail(
                    command=safe_command,
                    purpose=safe_purpose,
                    reason=safe_reason,
                    expected_outcome=safe_expected,
                ),
                "iteration": iteration if iteration > 0 else None,
                "tool_call_id": _as_str(tool_call_id),
                "action_id": _as_str(action_id),
                "command": safe_command,
                "purpose": safe_purpose,
                "plan": {
                    "commands": [safe_command],
                    "purpose": safe_purpose,
                    "reason": safe_reason,
                    "expected_outcome": safe_expected,
                    "title": _as_str(title).strip() or "执行命令",
                },
            },
        )

    async def _try_llm_repair_structured_spec(
        self,
        run: AgentRun,
        *,
        action_id: str,
        command_spec: Optional[Dict[str, Any]],
        purpose: str,
        title: str,
        failure_message: str,
    ) -> Dict[str, Any]:
        runtime_options = self._runtime_options(run)
        if not _sql_llm_repair_enabled():
            return {"status": "skipped", "reason": "llm_sql_repair_disabled"}
        if not bool(runtime_options.get("use_llm", True)):
            return {"status": "skipped", "reason": "llm_disabled_by_runtime_options"}
        safe_spec = normalize_followup_command_spec(command_spec)
        if not safe_spec:
            return {"status": "skipped", "reason": "missing_structured_spec"}
        args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
        safe_query = _as_str(args.get("query") or safe_spec.get("query") or safe_spec.get("sql")).strip()
        if not safe_query:
            return {"status": "skipped", "reason": "missing_query"}
        safe_tool = _as_str(safe_spec.get("tool")).strip().lower()
        if safe_tool not in {"kubectl_clickhouse_query", "k8s_clickhouse_query", "clickhouse_query"}:
            return {"status": "skipped", "reason": "unsupported_structured_tool"}

        prompt = (
            "你是 SQL 修复器。只输出 JSON，不要 markdown。\n"
            "返回 schema: {\"query\":\"...\"}\n"
            "目标：修复明显的 SQL token 粘连/空格缺失问题，保持语义不变，不要改业务过滤条件。\n"
            f"原始 SQL:\n{safe_query}\n"
            f"上下文标题: {_as_str(title)}\n"
            f"动作目的: {_as_str(purpose)}\n"
            f"最近失败: {_as_str(failure_message)}\n"
        )
        timeout_seconds = max(
            5,
            min(20, _as_int(os.getenv("AI_RUNTIME_SQL_LLM_REPAIR_TIMEOUT_SECONDS"), 8)),
        )
        try:
            llm_response = await asyncio.wait_for(
                get_llm_service().chat(
                    message=prompt,
                    context={
                        "task": "repair_structured_sql_query",
                        "action_id": action_id,
                        "purpose": _as_str(purpose),
                    },
                ),
                timeout=timeout_seconds,
            )
            parsed = _parse_llm_json_dict(llm_response, as_str=_as_str)
            if not isinstance(parsed, dict):
                return {"status": "skipped", "reason": "llm_non_json"}
            candidate_query = _as_str(parsed.get("query") or parsed.get("sql")).strip()
            if not candidate_query:
                return {"status": "skipped", "reason": "llm_empty_query"}
            repaired_query = _repair_clickhouse_query_text(candidate_query)
            if not repaired_query:
                return {"status": "skipped", "reason": "llm_repair_empty"}
            repaired_spec = {
                **safe_spec,
                "args": {
                    **args,
                    "query": repaired_query,
                },
                "query": repaired_query,
                "sql": repaired_query,
                "execution_sql": repaired_query,
                "display_sql": repaired_query,
            }
            compile_result = compile_followup_command_spec(repaired_spec, run_sql_preflight=True)
            if not bool(compile_result.get("ok")):
                return {
                    "status": "ask_user",
                    "failure_code": _as_str(compile_result.get("reason")).strip() or "sql_preflight_failed",
                    "failure_message": _as_str(compile_result.get("detail")).strip()
                    or _as_str(compile_result.get("reason")).strip()
                    or "sql_preflight_failed",
                }
            return {
                "status": "recovered",
                "command": _normalize_followup_command_line(compile_result.get("command")),
                "command_spec": (
                    compile_result.get("command_spec")
                    if isinstance(compile_result.get("command_spec"), dict)
                    else repaired_spec
                ),
                "recovery_kind": "llm_structured_sql_repair",
            }
        except Exception:
            return {"status": "skipped", "reason": "llm_repair_unavailable"}

    async def _bridge_command_run(
        self,
        *,
        run_id: str,
        exec_run_id: str,
        tool_call_id: str,
        title: str,
        tool_name: str,
        action_id: str = "",
        command: str = "",
        command_fingerprint: str = "",
        attempted_command_fingerprint: str = "",
        command_execution_key: str = "",
    ) -> None:
        if os.environ.get("PYTEST_CURRENT_TEST") is not None:
            result = bridge_exec_run_stream_to_runtime(
                runtime_service=self,
                run_id=run_id,
                exec_run_id=exec_run_id,
                tool_call_id=tool_call_id,
                title=title,
                tool_name=tool_name,
            )
        else:
            result = await asyncio.to_thread(
                bridge_exec_run_stream_to_runtime,
                runtime_service=self,
                run_id=run_id,
                exec_run_id=exec_run_id,
                tool_call_id=tool_call_id,
                title=title,
                tool_name=tool_name,
            )
        run = self.get_run(run_id)
        if run is None:
            return
        current_phase = "acting"
        final_status = _as_str((result or {}).get("status")).strip().lower()
        final_exit_code = _as_int((result or {}).get("exit_code"), 0)
        timed_out = (
            bool((result or {}).get("timed_out"))
            or final_status == "timed_out"
            or final_exit_code in {-9, -15}
        )
        if timed_out:
            final_status = "timed_out"
        terminal_status = final_status in {"completed", "cancelled", "failed", "timed_out"}
        if terminal_status:
            current_phase = "planning"
        observed_status = final_status or _as_str((result or {}).get("status")).strip().lower()
        if not observed_status and not terminal_status:
            observed_status = "running"
        summary = dict(run.summary_json or {})
        active_command_request = (
            summary.get("active_command_request")
            if isinstance(summary.get("active_command_request"), dict)
            else {}
        )
        summary_updates: Dict[str, Any] = {
            "current_phase": current_phase,
            "last_command_run_id": _as_str(exec_run_id),
            "last_tool_call_id": _as_str(tool_call_id),
            "last_action_id": _as_str(action_id),
            "last_command_status": observed_status,
            "last_command_timed_out": timed_out,
            "last_command_exit_code": final_exit_code,
            "last_command": _normalize_command_for_history(
                _as_str(active_command_request.get("command")).strip()
                or _as_str(command)
                or _as_str((result or {}).get("command"))
            ),
            "last_command_purpose": (
                _as_str(active_command_request.get("purpose")).strip()
                or _as_str((result or {}).get("purpose")).strip()
            ),
            "last_command_title": _as_str(active_command_request.get("title")).strip() or _as_str(title),
            "last_command_error_detail": (
                _as_str((result or {}).get("stderr")).strip()
                or _as_str((result or {}).get("error_detail")).strip()
            ),
        }
        if terminal_status:
            summary_updates.update(
                {
                    "active_command_run_id": "",
                    "active_command_fingerprint": "",
                    "active_command_execution_key": "",
                    "active_command_request": None,
                }
            )
        safe_execution_key = _as_str(command_execution_key).strip()
        if safe_execution_key:
            now_iso = utc_now_iso()
            index_entry: Dict[str, Any] = {
                "status": (observed_status or "running"),
                "command_run_id": _as_str(exec_run_id),
                "action_id": _as_str(action_id),
                "tool_call_id": _as_str(tool_call_id),
                "command": _normalize_command_for_history(
                    _as_str(active_command_request.get("command")).strip()
                    or _as_str(command)
                    or _as_str((result or {}).get("command"))
                ),
                "purpose": (
                    _as_str(active_command_request.get("purpose")).strip()
                    or _as_str((result or {}).get("purpose")).strip()
                ),
                "last_updated_at": now_iso,
            }
            if terminal_status:
                index_entry["finished_at"] = now_iso
                index_entry["exit_code"] = final_exit_code
                index_entry["timed_out"] = timed_out
            summary_updates["command_run_index"] = _upsert_command_run_index(
                summary.get("command_run_index"),
                command_execution_key=safe_execution_key,
                entry=index_entry,
                max_items=400,
            )
        if terminal_status and attempted_command_fingerprint:
            summary_updates["attempted_command_fingerprints"] = _merge_unique_text_items(
                summary.get("attempted_command_fingerprints"),
                attempted_command_fingerprint,
                max_items=600,
            )
        normalized_command = _normalize_command_for_history(command or (result or {}).get("command"))
        if terminal_status and final_status == "completed" and final_exit_code == 0 and normalized_command:
            summary_updates["executed_commands"] = _merge_unique_text_items(
                summary.get("executed_commands"),
                normalized_command,
                max_items=200,
            )
            if command_fingerprint:
                summary_updates["executed_command_fingerprints"] = _merge_unique_text_items(
                    summary.get("executed_command_fingerprints"),
                    command_fingerprint,
                    max_items=400,
                )
        # Phase 3 fix: record failed command fingerprints so the duplicate-check
        # gate can return needs_rethink instead of blindly re-dispatching
        if terminal_status and final_exit_code != 0 and command_fingerprint:
            summary_updates["failed_command_fingerprints"] = _merge_unique_text_items(
                summary.get("failed_command_fingerprints"),
                command_fingerprint,
                max_items=400,
            )
        if terminal_status and timed_out:
            runtime_options = self._runtime_options(run)
            timeout_recovery = attempt_timeout_recovery(
                command=_as_str(active_command_request.get("command")).strip() or normalized_command,
                command_spec=(
                    active_command_request.get("command_spec")
                    if isinstance(active_command_request.get("command_spec"), dict)
                    else None
                ),
                purpose=(
                    _as_str(active_command_request.get("purpose")).strip()
                    or _as_str((result or {}).get("purpose")).strip()
                ),
                recovery_history=summary.get("timeout_recovery_history"),
                max_rounds=max(1, _as_int(runtime_options.get("timeout_recovery_max_rounds"), 2)),
            )
            timeout_attempts = _as_list(timeout_recovery.get("recovery_attempts"))
            history_entry = (
                timeout_recovery.get("history_entry")
                if isinstance(timeout_recovery.get("history_entry"), dict)
                else None
            )
            summary_updates["last_timeout_recovery_attempts"] = len(timeout_attempts)
            summary_updates["last_timeout_recovery_variant"] = history_entry or {}
            summary_updates["last_timeout_recovery_history"] = timeout_attempts[-10:]
            if history_entry:
                summary_updates["timeout_recovery_history"] = _merge_timeout_recovery_history(
                    summary.get("timeout_recovery_history"),
                    history_entry,
                )
            self._update_run_summary(run, **summary_updates)
            if _as_str(timeout_recovery.get("status")).strip().lower() == "recovered":
                recovered_command = _normalize_followup_command_line(timeout_recovery.get("command"))
                recovered_command_spec = (
                    timeout_recovery.get("command_spec")
                    if isinstance(timeout_recovery.get("command_spec"), dict)
                    else None
                )
                self.append_event(
                    run.run_id,
                    event_protocol.ACTION_TIMEOUT_RECOVERY_SCHEDULED,
                    {
                        "tool_call_id": build_id("tool"),
                        "action_id": _as_str(active_command_request.get("action_id")).strip() or _as_str(action_id),
                        "previous_command_run_id": _as_str(exec_run_id),
                        "previous_command": _as_str(active_command_request.get("command")).strip() or normalized_command,
                        "recovery_kind": _as_str(timeout_recovery.get("recovery_kind")).strip(),
                        "attempts": len(timeout_attempts),
                        "message": _as_str(timeout_recovery.get("failure_message")).strip(),
                    },
                )
                await self.execute_command_tool(
                    run_id=run_id,
                    action_id=_as_str(active_command_request.get("action_id")).strip() or _as_str(action_id),
                    command=recovered_command,
                    command_spec=recovered_command_spec,
                    diagnosis_contract=(
                        active_command_request.get("diagnosis_contract")
                        if isinstance(active_command_request.get("diagnosis_contract"), dict)
                        else None
                    ),
                    purpose=(
                        _as_str(active_command_request.get("purpose")).strip()
                        or _as_str((result or {}).get("purpose")).strip()
                    ),
                    title=_as_str(active_command_request.get("title")).strip() or _as_str(title),
                    tool_name=_as_str(active_command_request.get("tool_name")).strip() or _as_str(tool_name),
                    confirmed=bool(active_command_request.get("confirmed")),
                    elevated=bool(active_command_request.get("elevated")),
                    confirmation_ticket=_as_str(active_command_request.get("confirmation_ticket")).strip(),
                    timeout_seconds=_as_int(active_command_request.get("timeout_seconds"), 20),
                )
                return
            if active_command_request:
                self._update_run_summary(
                    run,
                    pending_command_request={
                        **active_command_request,
                        "message": _as_str((result or {}).get("stderr")).strip() or "command timed out",
                        "status": "timed_out",
                    },
                )
            self._request_business_question(
                run,
                action_id=_as_str(active_command_request.get("action_id")).strip() or _as_str(action_id),
                command=_as_str(active_command_request.get("command")).strip() or normalized_command,
                purpose=(
                    _as_str(active_command_request.get("purpose")).strip()
                    or _as_str((result or {}).get("purpose")).strip()
                ),
                title=_as_str(active_command_request.get("title")).strip() or _as_str(title),
                failure_code=_as_str(timeout_recovery.get("failure_code")).strip() or "command_timed_out",
                failure_message=(
                    _as_str((result or {}).get("stderr")).strip()
                    or _as_str(timeout_recovery.get("failure_message")).strip()
                    or "command timed out"
                ),
                recovery_attempts=timeout_attempts,
            )
            return
        self._update_run_summary(run, **summary_updates)

    def _stage_normalize_command_execution(
        self,
        *,
        run_id: str,
        action_id: str,
        tool_call_id: str,
        command: str,
        command_spec: Optional[Dict[str, Any]],
        purpose: str,
        title: str,
        recovery_depth: int,
    ) -> Dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError("run not found")
        if is_terminal_run_status(run.status):
            raise RuntimeError("run is already terminal")
        self._ensure_no_pending_action(run)
        runtime_options = self._runtime_options(run)
        safe_tool_call_id = _as_str(tool_call_id) or build_id("tool")
        return {
            "run": run,
            "safe_tool_call_id": safe_tool_call_id,
            "safe_title": _as_str(title) or f"执行命令: {_as_str(command)[:80]}",
            "safe_action_id": _as_str(action_id) or safe_tool_call_id,
            "safe_purpose": _as_str(purpose).strip(),
            "structured_required": _require_structured_actions_enabled(),
            "safe_command_spec": normalize_followup_command_spec(command_spec),
            "runtime_options": runtime_options,
            "command_recovery_max_rounds": max(1, _as_int(runtime_options.get("command_recovery_max_rounds"), 2)),
            "safe_recovery_depth": max(0, _as_int(recovery_depth, 0)),
        }

    async def execute_command_tool(
        self,
        *,
        run_id: str,
        action_id: str = "",
        tool_call_id: str = "",
        command: str,
        command_spec: Optional[Dict[str, Any]] = None,
        diagnosis_contract: Optional[Dict[str, Any]] = None,
        purpose: str = "",
        title: str = "",
        tool_name: str = "command.exec",
        confirmed: bool = False,
        elevated: bool = False,
        confirmation_ticket: str = "",
        timeout_seconds: int = 20,
        recovery_depth: int = 0,
    ) -> Dict[str, Any]:
        context = self._stage_normalize_command_execution(
            run_id=run_id,
            action_id=action_id,
            tool_call_id=tool_call_id,
            command=command,
            command_spec=command_spec,
            purpose=purpose,
            title=title,
            recovery_depth=recovery_depth,
        )

        gate_result = await self._stage_apply_command_gates(
            run_id=run_id,
            raw_command=command,
            diagnosis_contract=diagnosis_contract,
            tool_name=tool_name,
            confirmation_ticket=confirmation_ticket,
            timeout_seconds=timeout_seconds,
            context=context,
        )
        if gate_result is not None:
            return gate_result

        idempotency_result = self._stage_check_command_idempotency(
            run_id=run_id,
            tool_name=tool_name,
            context=context,
        )
        if idempotency_result is not None:
            return idempotency_result

        return await self._stage_execute_command_run(
            run_id=run_id,
            diagnosis_contract=diagnosis_contract,
            tool_name=tool_name,
            confirmed=confirmed,
            elevated=elevated,
            confirmation_ticket=confirmation_ticket,
            timeout_seconds=timeout_seconds,
            context=context,
        )

    async def _stage_apply_command_gates(
        self,
        *,
        run_id: str,
        raw_command: str,
        diagnosis_contract: Optional[Dict[str, Any]],
        tool_name: str,
        confirmation_ticket: str,
        timeout_seconds: int,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        run = context["run"]
        safe_tool_call_id = context["safe_tool_call_id"]
        safe_title = context["safe_title"]
        safe_action_id = context["safe_action_id"]
        safe_purpose = context["safe_purpose"]
        structured_required = context["structured_required"]
        safe_command_spec = context["safe_command_spec"]
        runtime_options = context["runtime_options"]
        command_recovery_max_rounds = context["command_recovery_max_rounds"]
        safe_recovery_depth = context["safe_recovery_depth"]

        async def _return_waiting_for_structured_spec(
            message: str,
            current_command: str,
            current_command_spec: Optional[Dict[str, Any]],
            recovery_failure_code: str = "missing_or_invalid_command_spec",
        ) -> Dict[str, Any]:
            safe_message = _as_str(message).strip() or "缺少或无效的 command_spec，已阻断执行。"
            safe_recovery_failure_code = (
                _as_str(recovery_failure_code).strip().lower()
                or ("sql_preflight_failed" if "sql_preflight_failed" in safe_message else "missing_or_invalid_command_spec")
            )
            recovery_payload = build_command_spec_self_repair_payload(
                reason=safe_recovery_failure_code,
                detail=safe_message,
                command_spec=current_command_spec,
                raw_command=current_command,
            )
            self.append_event(
                run.run_id,
                "action_preflight_failed",
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "action_id": safe_action_id,
                    "command": current_command,
                    "purpose": safe_purpose,
                    "error_code": safe_recovery_failure_code,
                    "message": safe_message,
                    "recovery": recovery_payload,
                },
            )
            recovery = attempt_command_recovery(
                command=current_command,
                command_spec=current_command_spec,
                purpose=safe_purpose,
                failure_code=safe_recovery_failure_code,
                failure_message=safe_message,
                max_rounds=command_recovery_max_rounds,
            )
            if _as_str(recovery.get("status")).strip().lower() == "recovered":
                recovered_command = _normalize_followup_command_line(recovery.get("command")) or current_command
                recovered_command_spec = (
                    recovery.get("command_spec")
                    if isinstance(recovery.get("command_spec"), dict)
                    else current_command_spec
                )
                self._update_run_summary(
                    run,
                    last_recovery_failure_code=_as_str(recovery.get("failure_code")).strip(),
                    last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                    last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                    last_recovery_kind=_as_str(recovery.get("recovery_kind")).strip(),
                )
                self.append_event(
                    run.run_id,
                    "action_recovery_succeeded",
                    {
                        "tool_call_id": safe_tool_call_id,
                        "action_id": safe_action_id,
                        "command": recovered_command,
                        "purpose": safe_purpose,
                        "recovery_kind": _as_str(recovery.get("recovery_kind")).strip(),
                        "attempts": len(_as_list(recovery.get("recovery_attempts"))),
                    },
                )
                return {
                    "status": "recovered",
                    "command": recovered_command,
                    "command_spec": recovered_command_spec,
                }

            if safe_recovery_failure_code not in _NON_RECOVERABLE_STRUCTURED_FAILURE_CODES:
                llm_recovery = await self._try_llm_repair_structured_spec(
                    run,
                    action_id=safe_action_id,
                    command_spec=current_command_spec,
                    purpose=safe_purpose,
                    title=safe_title,
                    failure_message=_as_str(recovery.get("failure_message")).strip() or safe_message,
                )
                if _as_str(llm_recovery.get("status")).strip().lower() == "recovered":
                    recovered_command = _normalize_followup_command_line(llm_recovery.get("command")) or current_command
                    recovered_command_spec = (
                        llm_recovery.get("command_spec")
                        if isinstance(llm_recovery.get("command_spec"), dict)
                        else current_command_spec
                    )
                    self._update_run_summary(
                        run,
                        last_recovery_failure_code=_as_str(recovery.get("failure_code")).strip() or "sql_preflight_failed",
                        last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                        last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                        last_recovery_kind=_as_str(llm_recovery.get("recovery_kind")).strip() or "llm_structured_sql_repair",
                    )
                    self.append_event(
                        run.run_id,
                        "action_recovery_succeeded",
                        {
                            "tool_call_id": safe_tool_call_id,
                            "action_id": safe_action_id,
                            "command": recovered_command,
                            "purpose": safe_purpose,
                            "recovery_kind": _as_str(llm_recovery.get("recovery_kind")).strip() or "llm_structured_sql_repair",
                            "attempts": len(_as_list(recovery.get("recovery_attempts"))),
                        },
                    )
                    return {
                        "status": "recovered",
                        "command": recovered_command,
                        "command_spec": recovered_command_spec,
                    }

            final_failure_code = _as_str(recovery.get("failure_code")).strip() or safe_recovery_failure_code
            final_failure_message = _as_str(recovery.get("failure_message")).strip() or safe_message
            recovery_payload = build_command_spec_self_repair_payload(
                reason=final_failure_code,
                detail=final_failure_message,
                command_spec=current_command_spec,
                raw_command=current_command,
            )
            waiting = self._request_business_question(
                run,
                action_id=safe_action_id,
                command=current_command,
                purpose=safe_purpose or current_command,
                title=safe_title,
                failure_code=final_failure_code,
                failure_message=final_failure_message,
                recovery_attempts=_as_list(recovery.get("recovery_attempts")),
                recovery_context=recovery_payload,
            )
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_FINISHED,
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "status": "waiting_user_input",
                    "command": current_command,
                    "purpose": safe_purpose or current_command,
                    "message": safe_message,
                    "error_code": final_failure_code,
                    "recovery": recovery_payload,
                },
            )
            return {
                "status": "waiting_user_input",
                "tool_call_id": safe_tool_call_id,
                "run": (waiting or {}).get("run", run),
                "user_input_request": (waiting or {}).get("user_input_request", {}),
                "error": {
                    "code": final_failure_code,
                    "message": safe_message,
                    "recovery": recovery_payload,
                },
                "recovery": recovery_payload,
            }

        if safe_command_spec:
            compile_result = compile_followup_command_spec(safe_command_spec, run_sql_preflight=True)
            if not bool(compile_result.get("ok")):
                compile_reason = _as_str(compile_result.get("reason"), "compile failed")
                compile_detail = _as_str(compile_result.get("detail")).strip()
                detail = (
                    f"invalid command_spec: {compile_reason}"
                    if not compile_detail
                    else f"invalid command_spec: {compile_reason}: {compile_detail}"
                )
                if structured_required:
                    normalized_raw_command = _normalize_followup_command_line(raw_command) or _as_str(raw_command).strip()
                    structured_result = await _return_waiting_for_structured_spec(
                        detail,
                        normalized_raw_command,
                        safe_command_spec,
                        recovery_failure_code=compile_reason,
                    )
                    if _as_str(structured_result.get("status")).strip().lower() != "recovered":
                        return structured_result
                    safe_command_spec = (
                        structured_result.get("command_spec")
                        if isinstance(structured_result.get("command_spec"), dict)
                        else safe_command_spec
                    )
                    safe_command = _normalize_followup_command_line(structured_result.get("command")) or normalized_raw_command
                else:
                    raise ValueError(detail)
                if _as_str(structured_result.get("status")).strip().lower() == "recovered":
                    self.append_event(
                        run.run_id,
                        "action_spec_validated",
                        {
                            "tool_call_id": safe_tool_call_id,
                            "tool_name": _as_str(tool_name, "command.exec"),
                            "title": safe_title,
                            "action_id": safe_action_id,
                            "command": safe_command,
                            "purpose": safe_purpose,
                            "command_spec": safe_command_spec,
                        },
                    )
            else:
                safe_command_spec = (
                    compile_result.get("command_spec")
                    if isinstance(compile_result.get("command_spec"), dict)
                    else safe_command_spec
                )
                safe_command = _normalize_followup_command_line(compile_result.get("command"))
                self.append_event(
                    run.run_id,
                    "action_spec_validated",
                    {
                        "tool_call_id": safe_tool_call_id,
                        "tool_name": _as_str(tool_name, "command.exec"),
                        "title": safe_title,
                        "action_id": safe_action_id,
                        "command": safe_command,
                        "purpose": safe_purpose,
                        "command_spec": safe_command_spec,
                    },
                )
        else:
            safe_command = _normalize_followup_command_line(raw_command) or _as_str(raw_command).strip()
            if structured_required:
                structured_result = await _return_waiting_for_structured_spec(
                    "missing_or_invalid_command_spec: command_spec is required when AI_RUNTIME_REQUIRE_STRUCTURED_ACTIONS=true",
                    safe_command,
                    safe_command_spec,
                )
                if _as_str(structured_result.get("status")).strip().lower() != "recovered":
                    return structured_result
                safe_command = _normalize_followup_command_line(structured_result.get("command")) or safe_command
                safe_command_spec = (
                    structured_result.get("command_spec")
                    if isinstance(structured_result.get("command_spec"), dict)
                    else safe_command_spec
                )
        if not safe_command:
            raise ValueError("command is required")
        if not safe_purpose:
            raise ValueError("purpose is required")
        try:
            command_meta, _ = _resolve_followup_command_meta(safe_command)
        except Exception:
            command_meta = {}
        is_write_command = bool(command_meta.get("requires_write_permission")) or (
            _as_str(command_meta.get("command_type")).strip().lower() == "repair"
        )
        if _require_spec_for_repair_enabled() and not safe_command_spec:
            if is_write_command:
                self.append_event(
                    run.run_id,
                    event_protocol.TOOL_CALL_FINISHED,
                    {
                        "tool_call_id": safe_tool_call_id,
                        "tool_name": _as_str(tool_name, "command.exec"),
                        "title": safe_title,
                        "status": "failed",
                        "command": safe_command,
                        "purpose": safe_purpose,
                        "message": "高风险写命令需提供 command_spec（结构化命令）后才可审批执行。",
                        "command_type": _as_str(command_meta.get("command_type"), "repair"),
                        "risk_level": _as_str(command_meta.get("risk_level"), "high"),
                    },
                )
                return {
                    "status": "permission_required",
                    "tool_call_id": safe_tool_call_id,
                    "run": run,
                    "error": {
                        "code": "spec_required_for_repair",
                        "message": "高风险写命令需提供 command_spec（结构化命令）后才可审批执行。",
                    },
                }

        if _diagnosis_contract_gate_enabled() and is_write_command:
            diagnosis_reask_max_rounds = max(
                0,
                _as_int(runtime_options.get("diagnosis_contract_reask_max_rounds"), 1),
            )
            summary = dict(run.summary_json or {})
            current_reask_count = max(0, _as_int(summary.get("diagnosis_contract_reask_count"), 0))
            safe_diagnosis_contract = self._merge_diagnosis_contract_sources(
                run,
                explicit_contract=diagnosis_contract,
                command_spec=safe_command_spec,
            )
            missing_fields = _diagnosis_contract_missing_fields(safe_diagnosis_contract)
            if missing_fields:
                next_reask_count = current_reask_count + 1
                missing_text = _diagnosis_contract_missing_fields_text(missing_fields)
                pending_command_request = {
                    "tool_call_id": safe_tool_call_id,
                    "action_id": safe_action_id,
                    "command": safe_command,
                    "command_spec": safe_command_spec,
                    "purpose": safe_purpose,
                    "title": safe_title,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "timeout_seconds": int(timeout_seconds or 20),
                    "confirmation_ticket": _as_str(confirmation_ticket),
                    "diagnosis_contract": safe_diagnosis_contract,
                    "diagnosis_contract_missing_fields": missing_fields,
                }
                if current_reask_count >= diagnosis_reask_max_rounds:
                    self._persist_diagnosis_contract(
                        run,
                        contract=safe_diagnosis_contract,
                        missing_fields=missing_fields,
                        reask_count=next_reask_count,
                        reask_max_rounds=diagnosis_reask_max_rounds,
                        last_error="diagnosis_contract_incomplete",
                    )
                    blocked_run = self._block_run_for_diagnosis_contract_incomplete(
                        run,
                        tool_call_id=safe_tool_call_id,
                        tool_name=_as_str(tool_name, "command.exec"),
                        title=safe_title,
                        command=safe_command,
                        purpose=safe_purpose,
                        missing_fields=missing_fields,
                        attempts=next_reask_count,
                        max_rounds=diagnosis_reask_max_rounds,
                    )
                    return {
                        "status": "blocked",
                        "tool_call_id": safe_tool_call_id,
                        "run": blocked_run,
                        "error": {
                            "code": "diagnosis_contract_incomplete",
                            "message": (
                                "写命令被阻断：diagnosis_contract 缺少必填字段且超过重试上限，"
                                f"缺失字段：{missing_text}"
                            ),
                            "missing_fields": missing_fields,
                            "attempts": next_reask_count,
                            "max_reasks": diagnosis_reask_max_rounds,
                        },
                    }
                recovery = attempt_command_recovery(
                    command=safe_command,
                    command_spec=safe_command_spec,
                    diagnosis_contract=safe_diagnosis_contract,
                    purpose=safe_purpose,
                    failure_code="diagnosis_contract_incomplete",
                    failure_message=f"missing_fields={','.join(missing_fields)}",
                    max_rounds=command_recovery_max_rounds,
                )
                if _as_str(recovery.get("status")).strip().lower() == "recovered":
                    safe_diagnosis_contract = (
                        recovery.get("diagnosis_contract")
                        if isinstance(recovery.get("diagnosis_contract"), dict)
                        else safe_diagnosis_contract
                    )
                    missing_fields = _diagnosis_contract_missing_fields(safe_diagnosis_contract)
                if missing_fields:
                    pending_command_request["diagnosis_contract"] = safe_diagnosis_contract
                    pending_command_request["diagnosis_contract_missing_fields"] = missing_fields
                    self._persist_diagnosis_contract(
                        run,
                        contract=safe_diagnosis_contract,
                        missing_fields=missing_fields,
                        reask_count=next_reask_count,
                        reask_max_rounds=diagnosis_reask_max_rounds,
                        last_error="diagnosis_contract_incomplete",
                    )
                    self._update_run_summary(
                        run,
                        pending_command_request=pending_command_request,
                        diagnosis_contract_missing_fields=missing_fields,
                        diagnosis_contract_reask_count=next_reask_count,
                        diagnosis_contract_reask_max_rounds=diagnosis_reask_max_rounds,
                        last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                        last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                    )
                    waiting = self._request_business_question(
                        run,
                        action_id=safe_action_id,
                        command=safe_command,
                        purpose=safe_purpose,
                        title=safe_title,
                        failure_code="diagnosis_contract_incomplete",
                        failure_message=_as_str(recovery.get("failure_message")).strip() or missing_text,
                        missing_fields=missing_fields,
                        recovery_attempts=_as_list(recovery.get("recovery_attempts")),
                    )
                    self.append_event(
                        run.run_id,
                        event_protocol.TOOL_CALL_FINISHED,
                        {
                            "tool_call_id": safe_tool_call_id,
                            "tool_name": _as_str(tool_name, "command.exec"),
                            "title": safe_title,
                            "status": "waiting_user_input",
                            "command": safe_command,
                            "purpose": safe_purpose,
                            "message": (
                                "写命令暂未执行：diagnosis_contract 不完整，"
                                f"缺失字段：{missing_text}"
                            ),
                            "error_code": "diagnosis_contract_incomplete",
                            "missing_fields": missing_fields,
                        },
                    )
                    return {
                        "status": "waiting_user_input",
                        "tool_call_id": safe_tool_call_id,
                        "run": (waiting or {}).get("run", run),
                        "user_input_request": (waiting or {}).get("user_input_request", {}),
                        "error": {
                            "code": "diagnosis_contract_incomplete",
                            "message": f"diagnosis_contract missing fields: {missing_text}",
                            "missing_fields": missing_fields,
                            "attempts": next_reask_count,
                            "max_reasks": diagnosis_reask_max_rounds,
                        },
                    }
                self._update_run_summary(
                    run,
                    last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                    last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                )
            self._persist_diagnosis_contract(
                run,
                contract=safe_diagnosis_contract,
                missing_fields=[],
                reask_count=0,
                reask_max_rounds=diagnosis_reask_max_rounds,
                last_error="",
            )

        context["safe_command"] = safe_command
        context["safe_command_spec"] = safe_command_spec
        context["command_meta"] = command_meta if isinstance(command_meta, dict) else {}
        return None

    def _stage_check_command_idempotency(
        self,
        *,
        run_id: str,
        tool_name: str,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        run = context["run"]
        safe_tool_call_id = context["safe_tool_call_id"]
        safe_title = context["safe_title"]
        safe_action_id = context["safe_action_id"]
        safe_purpose = context["safe_purpose"]
        safe_command = context["safe_command"]
        safe_command_spec = context["safe_command_spec"]
        normalized_command = _normalize_command_for_history(safe_command)
        target_info = _resolve_command_target(safe_command_spec if isinstance(safe_command_spec, dict) else None)
        target_kind = _as_str(target_info.get("target_kind")).strip()
        target_identity = _as_str(target_info.get("target_identity")).strip()
        command_execution_key = _build_command_execution_key(
            run_id=run_id,
            action_id=safe_action_id,
            command=normalized_command,
            purpose=safe_purpose,
            target_identity=target_identity,
        )
        command_fingerprint = _build_command_fingerprint(
            command=normalized_command,
            purpose=safe_purpose,
            action_id=safe_action_id,
        )
        attempted_command_fingerprint = _build_attempt_fingerprint(
            command=normalized_command,
            purpose=safe_purpose,
            action_id=safe_action_id,
        )
        summary = dict(run.summary_json or {})
        active_command_run_id = _as_str(summary.get("active_command_run_id"))
        if active_command_run_id:
            return {
                "status": "running_existing",
                "tool_call_id": safe_tool_call_id,
                "command_run_id": active_command_run_id,
                "run": run,
            }
        command_run_index = _normalize_command_run_index(summary.get("command_run_index"), max_items=400)
        indexed_entry = (
            command_run_index.get(command_execution_key)
            if command_execution_key
            else None
        )
        indexed_status = _as_str((indexed_entry or {}).get("status")).strip().lower()
        indexed_run_id = _as_str((indexed_entry or {}).get("command_run_id")).strip()
        indexed_exit_code = _as_int((indexed_entry or {}).get("exit_code"), 1)
        if indexed_status in {"running", "queued", "submitted", "pending"} and indexed_run_id:
            return {
                "status": "running_existing",
                "tool_call_id": safe_tool_call_id,
                "command_run_id": indexed_run_id,
                "run": run,
            }
        if indexed_status in {"completed", "succeeded", "success"} and indexed_run_id and indexed_exit_code == 0:
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "status": "skipped_duplicate",
                    "reason_code": "duplicate_skipped",
                    "action_id": safe_action_id,
                    "command": safe_command,
                    "purpose": safe_purpose,
                    "message": "同一 run 已命中幂等执行键，跳过重复执行。",
                    "command_run_id": indexed_run_id,
                    "reused_command_run_id": indexed_run_id,
                    "evidence_reuse": True,
                    "evidence_outcome": "reused",
                    "reused_evidence_ids": [indexed_run_id] if indexed_run_id else [],
                },
            )
            return {
                "status": "skipped_duplicate",
                "tool_call_id": safe_tool_call_id,
                "command_run_id": indexed_run_id,
                "run": run,
            }
        # Phase 3 fix: if this exact command previously FAILED, signal needs_rethink
        # so the LangGraph inner-loop gets a chance to generate an alternative action
        # instead of blindly re-dispatching the same broken command.
        failed_fingerprints = {
            _as_str(item).strip()
            for item in _as_list(summary.get("failed_command_fingerprints"))
            if _as_str(item).strip()
        }
        if command_fingerprint and command_fingerprint in failed_fingerprints:
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "status": "needs_rethink",
                    "reason_code": "previously_failed_command",
                    "action_id": safe_action_id,
                    "command": safe_command,
                    "purpose": safe_purpose,
                    "message": (
                        "该命令在本次 run 中已执行并失败，需要重新规划替代方案，"
                        "拒绝重复执行相同失败命令。"
                    ),
                },
            )
            return {
                "status": "needs_rethink",
                "tool_call_id": safe_tool_call_id,
                "run": run,
                "reason": "previously_failed_command",
            }
        executed_fingerprints = {
            _as_str(item).strip()
            for item in _as_list(summary.get("executed_command_fingerprints"))
            if _as_str(item).strip()
        }
        if command_fingerprint and command_fingerprint in executed_fingerprints:
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "status": "skipped_duplicate",
                    "reason_code": "duplicate_skipped",
                    "action_id": safe_action_id,
                    "command": safe_command,
                    "purpose": safe_purpose,
                    "message": "同一 run 已执行过该命令，跳过重复执行。",
                    "evidence_reuse": True,
                    "evidence_outcome": "reused",
                },
            )
            return {
                "status": "skipped_duplicate",
                "tool_call_id": safe_tool_call_id,
                "run": run,
            }
        context["normalized_command"] = normalized_command
        context["target_kind"] = target_kind
        context["target_identity"] = target_identity
        context["command_execution_key"] = command_execution_key
        context["command_fingerprint"] = command_fingerprint
        context["attempted_command_fingerprint"] = attempted_command_fingerprint
        context["summary"] = summary
        return None

    async def _stage_execute_command_run(
        self,
        *,
        run_id: str,
        diagnosis_contract: Optional[Dict[str, Any]],
        tool_name: str,
        confirmed: bool,
        elevated: bool,
        confirmation_ticket: str,
        timeout_seconds: int,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        run = context["run"]
        safe_tool_call_id = context["safe_tool_call_id"]
        safe_title = context["safe_title"]
        safe_action_id = context["safe_action_id"]
        safe_purpose = context["safe_purpose"]
        safe_command = context["safe_command"]
        safe_command_spec = context["safe_command_spec"]
        command_meta = context["command_meta"]
        runtime_options = context["runtime_options"]
        command_recovery_max_rounds = context["command_recovery_max_rounds"]
        safe_recovery_depth = context["safe_recovery_depth"]
        normalized_command = context["normalized_command"]
        target_kind = context["target_kind"]
        target_identity = context["target_identity"]
        command_execution_key = context["command_execution_key"]
        command_fingerprint = context["command_fingerprint"]
        attempted_command_fingerprint = context["attempted_command_fingerprint"]
        summary = context["summary"]

        self._emit_pre_command_plan(
            run,
            tool_call_id=safe_tool_call_id,
            action_id=safe_action_id,
            command=safe_command,
            purpose=safe_purpose,
            title=safe_title,
            command_meta=command_meta if isinstance(command_meta, dict) else {},
        )

        safe_confirmation_ticket = _as_str(confirmation_ticket)
        response = await create_command_run(
            session_id=run.session_id,
            message_id=run.assistant_message_id,
            action_id=safe_action_id,
            command=safe_command,
            command_spec=safe_command_spec if isinstance(safe_command_spec, dict) else None,
            purpose=safe_purpose,
            step_id=safe_action_id,
            confirmed=bool(confirmed),
            elevated=bool(elevated),
            confirmation_ticket=safe_confirmation_ticket,
            approval_token=safe_confirmation_ticket,
            timeout_seconds=int(timeout_seconds or 20),
            target_kind=target_kind,
            target_identity=target_identity,
        )
        auto_retried = False
        initial_status = _as_str((response or {}).get("status")).strip().lower()
        retry_reason = _as_str(
            (response or {}).get("message")
            or (response or {}).get("detail")
            or (response or {}).get("reason")
        ).strip().lower()
        if (
            bool(confirmed)
            and initial_status in {"confirmation_required", "elevation_required"}
            and "confirmation ticket invalid" in retry_reason
        ):
            retry_ticket = _as_str((response or {}).get("confirmation_ticket")).strip()
            if retry_ticket and retry_ticket != safe_confirmation_ticket:
                response = await create_command_run(
                    session_id=run.session_id,
                    message_id=run.assistant_message_id,
                    action_id=safe_action_id,
                    command=safe_command,
                    command_spec=safe_command_spec if isinstance(safe_command_spec, dict) else None,
                    purpose=safe_purpose,
                    step_id=safe_action_id,
                    confirmed=bool(confirmed),
                    elevated=bool(elevated),
                    confirmation_ticket=retry_ticket,
                    approval_token=retry_ticket,
                    timeout_seconds=int(timeout_seconds or 20),
                    target_kind=target_kind,
                    target_identity=target_identity,
                )
                auto_retried = True

        command_run = response.get("run") if isinstance(response, dict) else None
        if isinstance(command_run, dict):
            command_run_id = _as_str(command_run.get("run_id"))
            active_command_request = {
                "tool_call_id": safe_tool_call_id,
                "action_id": safe_action_id,
                "command": safe_command,
                "command_spec": safe_command_spec if isinstance(safe_command_spec, dict) else None,
                "diagnosis_contract": self._merge_diagnosis_contract_sources(
                    run,
                    explicit_contract=diagnosis_contract,
                    command_spec=safe_command_spec,
                ),
                "purpose": safe_purpose,
                "title": safe_title,
                "tool_name": _as_str(tool_name, "command.exec"),
                "timeout_seconds": int(timeout_seconds or 20),
                "confirmation_ticket": safe_confirmation_ticket,
                "confirmed": bool(confirmed),
                "elevated": bool(elevated),
                "target_kind": target_kind,
                "target_identity": target_identity,
                "command_execution_key": command_execution_key,
            }
            latest_summary = dict(run.summary_json or {})
            command_index_entry = {
                "status": "running",
                "command_run_id": command_run_id,
                "action_id": safe_action_id,
                "tool_call_id": safe_tool_call_id,
                "command": normalized_command,
                "purpose": safe_purpose,
                "target_identity": target_identity,
                "created_at": utc_now_iso(),
                "last_updated_at": utc_now_iso(),
            }
            self._update_run_summary(
                run,
                current_phase="acting",
                active_command_run_id=command_run_id,
                active_command_fingerprint=command_fingerprint,
                active_command_execution_key=command_execution_key,
                last_tool_call_id=safe_tool_call_id,
                active_command_request=active_command_request,
                attempted_command_fingerprints=_merge_unique_text_items(
                    latest_summary.get("attempted_command_fingerprints"),
                    attempted_command_fingerprint,
                    max_items=600,
                ),
                command_run_index=_upsert_command_run_index(
                    latest_summary.get("command_run_index"),
                    command_execution_key=command_execution_key,
                    entry=command_index_entry,
                    max_items=400,
                ),
            )
            task = asyncio.create_task(
                self._bridge_command_run(
                    run_id=run_id,
                    exec_run_id=command_run_id,
                    tool_call_id=safe_tool_call_id,
                    title=safe_title,
                    tool_name=_as_str(tool_name, "command.exec"),
                    action_id=safe_action_id,
                    command=safe_command,
                    command_fingerprint=command_fingerprint,
                    attempted_command_fingerprint=attempted_command_fingerprint,
                    command_execution_key=command_execution_key,
                )
            )
            self._register_background_task(run_id, task)
            result = {
                "status": "running",
                "tool_call_id": safe_tool_call_id,
                "command_run_id": command_run_id,
                "run": run,
                "command_run": command_run,
            }
            if auto_retried:
                result["auto_retried"] = True
            return result

        status = _as_str((response or {}).get("status")).lower()
        if _as_str((response or {}).get("command_type")).strip().lower() not in {"", "unknown"}:
            self._update_run_summary(
                run,
                unknown_semantics_attempts=0,
                unknown_semantics_last_reason="",
            )
        if status in {"elevation_required", "confirmation_required"}:
            pending_command_request = {
                "tool_call_id": safe_tool_call_id,
                "action_id": safe_action_id,
                "command": safe_command,
                "command_spec": safe_command_spec,
                "diagnosis_contract": self._merge_diagnosis_contract_sources(
                    run,
                    explicit_contract=diagnosis_contract,
                    command_spec=safe_command_spec,
                ),
                "purpose": safe_purpose,
                "title": safe_title,
                "tool_name": _as_str(tool_name, "command.exec"),
                "timeout_seconds": int(timeout_seconds or 20),
                "confirmation_ticket": _as_str((response or {}).get("confirmation_ticket")),
                "command_fingerprint": command_fingerprint,
                "command_execution_key": command_execution_key,
            }
            self._update_run_summary(run, pending_command_request=pending_command_request)
            approval_payload = build_approval_required_payload(
                tool_call_id=safe_tool_call_id,
                action_id=safe_action_id,
                command=safe_command,
                purpose=safe_purpose,
                precheck=response if isinstance(response, dict) else {},
            )
            approval = self.request_approval(
                run_id,
                approval_id=_as_str(approval_payload.get("confirmation_ticket")) or build_id("apr"),
                title=safe_title,
                reason=_as_str((response or {}).get("message")),
                command=safe_command,
                purpose=safe_purpose,
                command_type=_as_str(approval_payload.get("command_type"), "unknown"),
                risk_level=_as_str(approval_payload.get("risk_level"), "high"),
                command_family=_as_str(approval_payload.get("command_family"), "unknown"),
                approval_policy=_as_str(approval_payload.get("approval_policy"), "elevation_required"),
                executor_type=_as_str(approval_payload.get("executor_type"), "local_process"),
                executor_profile=_as_str(approval_payload.get("executor_profile"), "local-default"),
                target_kind=_as_str(approval_payload.get("target_kind"), "runtime_node"),
                target_identity=_as_str(approval_payload.get("target_identity"), "runtime:local"),
                requires_confirmation=bool(approval_payload.get("requires_confirmation")),
                requires_elevation=bool(approval_payload.get("requires_elevation")),
            )
            return {
                "status": status,
                "tool_call_id": safe_tool_call_id,
                "approval": (approval or {}).get("approval", approval_payload),
                "run": (approval or {}).get("run", run),
            }

        command_type = _as_str((response or {}).get("command_type")).strip().lower()
        if status in {"permission_required", "failed", "unknown"} and command_type in {"", "unknown"}:
            max_unknown_retries = max(0, _as_int(runtime_options.get("unknown_semantics_max_retries"), 1))
            previous_summary = dict(run.summary_json or {})
            current_unknown_attempts = max(0, _as_int(previous_summary.get("unknown_semantics_attempts"), 0))
            next_unknown_attempts = current_unknown_attempts + 1
            unknown_reason = _as_str(
                (response or {}).get("message"),
                "当前动作未提供可执行命令",
            ) or "当前动作未提供可执行命令"

            if current_unknown_attempts >= max_unknown_retries:
                now_iso = utc_now_iso()
                previous_status = run.status
                previous_summary.pop("pending_action", None)
                previous_summary.pop("pending_user_input", None)
                previous_summary.pop("pending_command_request", None)
                run.status = RUN_STATUS_BLOCKED
                run.error_code = "unknown_semantics_exceeded"
                run.error_detail = (
                    f"unknown semantics exceeded retry limit "
                    f"(attempts={next_unknown_attempts}, max_retries={max_unknown_retries}): {unknown_reason}"
                )
                run.updated_at = now_iso
                run.ended_at = now_iso
                run.summary_json = {
                    **previous_summary,
                    "current_phase": "blocked",
                    "blocked_reason": "unknown_semantics_exceeded",
                    "pending_approval_count": 0,
                    "unknown_semantics_attempts": next_unknown_attempts,
                    "unknown_semantics_max_retries": max_unknown_retries,
                    "unknown_semantics_last_reason": unknown_reason,
                }
                self.store.save_run(run)
                self.append_event(
                    run.run_id,
                    event_protocol.RUN_STATUS_CHANGED,
                    {
                        "status": run.status,
                        "previous_status": previous_status,
                        "current_phase": "blocked",
                        "blocked_reason": "unknown_semantics_exceeded",
                    },
                )
                self.append_event(
                    run.run_id,
                    event_protocol.TOOL_CALL_FINISHED,
                    {
                        "tool_call_id": safe_tool_call_id,
                        "tool_name": _as_str(tool_name, "command.exec"),
                        "title": safe_title,
                        "status": "failed",
                        "command": safe_command,
                        "purpose": safe_purpose,
                        "message": unknown_reason,
                        "error_code": "unknown_semantics_exceeded",
                    },
                )
                return {
                    "status": "blocked",
                    "tool_call_id": safe_tool_call_id,
                    "run": run,
                    "error": {
                        "code": "unknown_semantics_exceeded",
                        "message": unknown_reason,
                        "attempts": next_unknown_attempts,
                        "max_retries": max_unknown_retries,
                    },
                }

            pending_command_request = {
                "tool_call_id": safe_tool_call_id,
                "action_id": safe_action_id,
                "command": safe_command,
                "command_spec": safe_command_spec,
                "diagnosis_contract": self._merge_diagnosis_contract_sources(
                    run,
                    explicit_contract=diagnosis_contract,
                    command_spec=safe_command_spec,
                ),
                "purpose": safe_purpose,
                "title": safe_title,
                "tool_name": _as_str(tool_name, "command.exec"),
                "status": status or "failed",
                "message": unknown_reason,
                "command_fingerprint": command_fingerprint,
                "command_execution_key": command_execution_key,
            }
            normalized_unknown_reason = unknown_reason.lower()
            normalized_failure_reason = normalize_followup_reason_code(unknown_reason)
            if "sql_preflight_failed" in normalized_unknown_reason:
                unknown_failure_code = "sql_preflight_failed"
            elif normalized_failure_reason != "other":
                unknown_failure_code = normalized_failure_reason
            else:
                unknown_failure_code = "unknown_semantics"
            recovery = attempt_command_recovery(
                command=safe_command,
                command_spec=safe_command_spec,
                diagnosis_contract=self._merge_diagnosis_contract_sources(
                    run,
                    explicit_contract=diagnosis_contract,
                    command_spec=safe_command_spec,
                ),
                purpose=safe_purpose,
                failure_code=unknown_failure_code,
                failure_message=unknown_reason,
                max_rounds=command_recovery_max_rounds,
            )
            if _as_str(recovery.get("status")).strip().lower() == "recovered":
                recovered_command = _normalize_followup_command_line(recovery.get("command")) or safe_command
                recovered_command_spec = (
                    recovery.get("command_spec")
                    if isinstance(recovery.get("command_spec"), dict)
                    else safe_command_spec
                )
                if (
                    (recovered_command != safe_command or recovered_command_spec != safe_command_spec)
                    and safe_recovery_depth < command_recovery_max_rounds
                ):
                    self._update_run_summary(
                        run,
                        last_recovery_failure_code="unknown_semantics",
                        last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                        last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                        last_recovery_kind=_as_str(recovery.get("recovery_kind")).strip(),
                    )
                    return await self.execute_command_tool(
                        run_id=run_id,
                        action_id=safe_action_id,
                        tool_call_id=safe_tool_call_id,
                        command=recovered_command,
                        command_spec=recovered_command_spec,
                        diagnosis_contract=diagnosis_contract,
                        purpose=safe_purpose,
                        title=safe_title,
                        tool_name=tool_name,
                        confirmed=confirmed,
                        elevated=elevated,
                        confirmation_ticket=confirmation_ticket,
                        timeout_seconds=timeout_seconds,
                        recovery_depth=safe_recovery_depth + 1,
                    )
            llm_recovery = await self._try_llm_repair_structured_spec(
                run,
                action_id=safe_action_id,
                command_spec=safe_command_spec,
                purpose=safe_purpose,
                title=safe_title,
                failure_message=_as_str(recovery.get("failure_message")).strip() or unknown_reason,
            )
            if _as_str(llm_recovery.get("status")).strip().lower() == "recovered":
                recovered_command = _normalize_followup_command_line(llm_recovery.get("command")) or safe_command
                recovered_command_spec = (
                    llm_recovery.get("command_spec")
                    if isinstance(llm_recovery.get("command_spec"), dict)
                    else safe_command_spec
                )
                if (
                    (recovered_command != safe_command or recovered_command_spec != safe_command_spec)
                    and safe_recovery_depth < command_recovery_max_rounds
                ):
                    self._update_run_summary(
                        run,
                        last_recovery_failure_code=unknown_failure_code,
                        last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                        last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
                        last_recovery_kind=_as_str(llm_recovery.get("recovery_kind")).strip() or "llm_structured_sql_repair",
                    )
                    return await self.execute_command_tool(
                        run_id=run_id,
                        action_id=safe_action_id,
                        tool_call_id=safe_tool_call_id,
                        command=recovered_command,
                        command_spec=recovered_command_spec,
                        diagnosis_contract=diagnosis_contract,
                        purpose=safe_purpose,
                        title=safe_title,
                        tool_name=tool_name,
                        confirmed=confirmed,
                        elevated=elevated,
                        confirmation_ticket=confirmation_ticket,
                        timeout_seconds=timeout_seconds,
                        recovery_depth=safe_recovery_depth + 1,
                    )
            self._update_run_summary(
                run,
                pending_command_request=pending_command_request,
                unknown_semantics_attempts=next_unknown_attempts,
                unknown_semantics_max_retries=max_unknown_retries,
                unknown_semantics_last_reason=unknown_reason,
                last_recovery_attempts=len(_as_list(recovery.get("recovery_attempts"))),
                last_recovery_attempt_history=_as_list(recovery.get("recovery_attempts"))[-10:],
            )
            waiting = self._request_business_question(
                run,
                action_id=safe_action_id,
                command=safe_command,
                purpose=safe_purpose,
                title=safe_title,
                failure_code=_as_str(recovery.get("failure_code")).strip() or unknown_failure_code,
                failure_message=_as_str(recovery.get("failure_message")).strip() or unknown_reason,
                recovery_attempts=_as_list(recovery.get("recovery_attempts")),
            )
            return {
                "status": "waiting_user_input",
                "tool_call_id": safe_tool_call_id,
                "run": (waiting or {}).get("run", run),
                "user_input_request": (waiting or {}).get("user_input_request", {}),
            }

        if status in {"permission_required", "failed"} and attempted_command_fingerprint:
            self._update_run_summary(
                run,
                attempted_command_fingerprints=_merge_unique_text_items(
                    summary.get("attempted_command_fingerprints"),
                    attempted_command_fingerprint,
                    max_items=600,
                ),
            )

        self.append_event(
            run_id,
            event_protocol.TOOL_CALL_FINISHED,
            {
                "tool_call_id": safe_tool_call_id,
                "tool_name": _as_str(tool_name, "command.exec"),
                "title": safe_title,
                "status": status or "failed",
                "command": safe_command,
                "purpose": safe_purpose,
                "message": _as_str((response or {}).get("message")),
            },
        )
        return {
            "status": status or "failed",
            "tool_call_id": safe_tool_call_id,
            "run": run,
            "error": response if isinstance(response, dict) else {},
        }

    def cancel_run(self, run_id: str, reason: str = "user_cancelled") -> Optional[AgentRun]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            return run
        self._cancel_pending_action_timer(run_id)
        now_iso = utc_now_iso()
        previous_status = run.status
        run.status = RUN_STATUS_CANCELLED
        run.error_code = "cancelled"
        run.error_detail = _as_str(reason, "user_cancelled")
        run.updated_at = now_iso
        run.ended_at = now_iso
        run.summary_json = {
            **(run.summary_json or {}),
            "current_phase": "cancelled",
        }
        run.summary_json.pop("pending_action", None)
        run.summary_json.pop("pending_approval", None)
        run.summary_json.pop("pending_user_input", None)
        self.store.save_run(run)
        self.append_event(
            run.run_id,
            event_protocol.RUN_STATUS_CHANGED,
            {"status": run.status, "previous_status": previous_status},
        )
        self.append_event(
            run.run_id,
            event_protocol.RUN_CANCELLED,
            {"status": run.status, "reason": run.error_detail},
        )
        return run

    async def cancel_active_command_run(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        command_run_id = _as_str((run.summary_json or {}).get("active_command_run_id"))
        if not command_run_id:
            return
        try:
            await cancel_command_run(command_run_id)
        except Exception:
            return

    async def interrupt_run(self, run_id: str, *, reason: str = "user_interrupt_esc") -> Optional[AgentRun]:
        run = self.get_run(run_id)
        if run is None:
            return None
        if is_terminal_run_status(run.status):
            return run
        self.append_event(
            run.run_id,
            event_protocol.RUN_INTERRUPTED,
            {
                "reason": _as_str(reason, "user_interrupt_esc"),
                "status": run.status,
                "active_command_run_id": _as_str((run.summary_json or {}).get("active_command_run_id")),
            },
        )
        await self.cancel_active_command_run(run_id)
        return self.cancel_run(run_id, reason=_as_str(reason, "user_interrupt_esc"))


_agent_runtime_service: Optional[AgentRuntimeService] = None


def get_agent_runtime_service(storage_adapter: Any = None) -> AgentRuntimeService:
    global _agent_runtime_service
    if _agent_runtime_service is None:
        _agent_runtime_service = AgentRuntimeService(storage_adapter=storage_adapter)
    elif storage_adapter is not None and not _agent_runtime_service.store.storage:
        _agent_runtime_service.attach_storage(storage_adapter)
    return _agent_runtime_service
