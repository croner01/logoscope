"""
Phase 4 & 6: Auto-Remediation Controller

Phase 4 — ``build_and_save_remediation_plan()``:
    Called by service.py's ``_try_auto_save_remediation_plan()`` at the end of
    every completed diagnostic run.  Extracts a RemediationPlan from the run's
    evidence/summary, saves it to the CaseStore as a pending-verification case,
    and emits a structured log entry so operators are notified.

Phase 6 — ``execute_authorized_auto_fix()``:
    Called when a human has pre-authorized a remediation plan via the API
    (POST /api/ai/remediation/{case_id}/authorize-auto-fix).
    Iterates the plan's RemediationSteps, dispatches each via the runtime's
    execute_command_tool(), and records the outcome.

Both phases enforce the **human-in-the-loop** contract:
    - Phase 4 saves plans with ``auto_fix_authorized=False`` (pending human review)
    - Phase 6 only executes plans where ``auto_fix_authorized=True``
    - Execution is logged to the case change-history for full auditability
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ai.agent_runtime.models import AgentRun
    from ai.agent_runtime.service import AgentRuntimeService

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


# ──────────────────────────────────────────────────────────────────────────────
# Evidence extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_evidence_summary(run: "AgentRun") -> Dict[str, Any]:
    """
    Pull the key diagnostic artefacts from the run's summary_json.

    Returns a dict with:
      - evidence_snippets: list of successful step snippets
      - failed_steps: list of {step_id, failure_category, command}
      - service_name: inferred from skill_context
      - components: unique list of services mentioned in evidence
      - error_keywords: keywords extracted from failed-step messages
    """
    summary = run.summary_json if isinstance(run.summary_json, dict) else {}

    # The inner graph state is often stored in summary["inner_state"]
    inner_state = summary.get("inner_state") or {}
    evidence_list: List[Dict[str, Any]] = _as_list(inner_state.get("evidence"))
    skill_context: Dict[str, Any] = inner_state.get("skill_context") or {}

    service_name = _as_str(skill_context.get("service_name"))
    namespace = _as_str(skill_context.get("namespace"), "islap")

    snippets: List[str] = []
    failed_steps: List[Dict[str, Any]] = []
    components: List[str] = []
    error_keywords: List[str] = []

    for entry in evidence_list:
        if not isinstance(entry, dict):
            continue
        sname = _as_str(entry.get("skill_name"))
        if sname and sname not in components:
            components.append(sname)

        if entry.get("success"):
            snippet = _as_str(entry.get("snippet", ""))[:400]
            if snippet:
                snippets.append(f"[{entry.get('step_id')}] {snippet}")
        else:
            fc = _as_str(entry.get("failure_category"))
            if fc:
                failed_steps.append({
                    "step_id": _as_str(entry.get("step_id")),
                    "failure_category": fc,
                    "command": _as_str(entry.get("command")),
                    "snippet": _as_str(entry.get("snippet", ""))[:200],
                })
                # Extract keywords from the failure snippet
                _extract_error_keywords(
                    _as_str(entry.get("snippet", "")), error_keywords
                )

    return {
        "service_name": service_name,
        "namespace": namespace,
        "snippets": snippets,
        "failed_steps": failed_steps,
        "components": list(dict.fromkeys(components)),  # deduplicated
        "error_keywords": list(dict.fromkeys(error_keywords)),
        "skill_context": skill_context,
    }


def _extract_error_keywords(text: str, target: List[str]) -> None:
    """Append notable error keywords from *text* into *target* list."""
    import re
    keyword_patterns = [
        r"\b(OOM|OutOfMemory|MemoryError)\b",
        r"\b(ConnectionRefused|connection refused)\b",
        r"\b(Timeout|timed? out)\b",
        r"\b(NotFound|not found)\b",
        r"\b(Forbidden|permission denied)\b",
        r"\b(CrashLoopBackOff|ImagePullBackOff)\b",
        r"\b(ERROR|FATAL|CRITICAL)\b",
        r"exception:\s*(\w+Exception)",
        r"error code[:\s]+([A-Z_]+\d*)",
    ]
    for pat in keyword_patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            kw = match.group(1) if match.lastindex else match.group(0)
            kw_lower = kw.lower().strip()
            if kw_lower and kw_lower not in [k.lower() for k in target]:
                target.append(kw.strip())


def _infer_error_category(failed_steps: List[Dict[str, Any]]) -> str:
    """Infer the dominant error category from a list of failed steps."""
    if not failed_steps:
        return "unknown"
    category_counts: Dict[str, int] = {}
    for step in failed_steps:
        cat = _as_str(step.get("failure_category", "unknown"))
        category_counts[cat] = category_counts.get(cat, 0) + 1
    return max(category_counts, key=lambda k: category_counts[k])


def _build_remediation_steps_from_evidence(
    evidence_summary: Dict[str, Any],
    run_question: str,
) -> List[Dict[str, Any]]:
    """
    Build a list of human-readable remediation step dicts from the diagnostic
    evidence.  These are *suggestions* for human review — not automatic actions.

    Each step dict is compatible with ``RemediationStep.to_dict()``.
    """
    steps: List[Dict[str, Any]] = []
    failed_steps = evidence_summary.get("failed_steps") or []
    service = evidence_summary.get("service_name") or "unknown-service"
    namespace = evidence_summary.get("namespace") or "islap"

    for i, fstep in enumerate(failed_steps, start=1):
        fc = _as_str(fstep.get("failure_category"))
        step_id = _as_str(fstep.get("step_id"))
        command = _as_str(fstep.get("command"))

        if fc == "resource_not_found":
            steps.append({
                "action": (
                    f"确认 {service} 的 K8s 资源是否存在："
                    f" kubectl get all -n {namespace} -l app={service}"
                ),
                "verification": f"确认输出中包含 {service} 相关 Pod/Deployment",
                "rollback": "无需回滚，仅查询操作",
                "risk_level": "low",
                "auto_fixable": False,
            })
        elif fc == "permission_denied":
            steps.append({
                "action": (
                    f"检查 {service} ServiceAccount 的 RBAC 权限："
                    f" kubectl describe clusterrolebinding -l app={service}"
                ),
                "verification": "确认 ServiceAccount 绑定了所需的 ClusterRole/Role",
                "rollback": "如需授权，先在测试环境验证再执行",
                "risk_level": "medium",
                "auto_fixable": False,
            })
        elif fc == "command_syntax_error":
            steps.append({
                "action": (
                    f"核实失败命令语法并手动修正（步骤 {step_id}）：\n  {command}"
                ),
                "verification": "命令成功执行且有预期输出",
                "rollback": "无需回滚，调整语法后重新执行",
                "risk_level": "low",
                "auto_fixable": False,
            })
        elif fc == "connection_failure":
            steps.append({
                "action": (
                    f"检查 {service} 网络连通性："
                    f" kubectl get svc -n {namespace} -l app={service}"
                    f" && kubectl get endpoints -n {namespace} -l app={service}"
                ),
                "verification": "Endpoints 包含就绪 Pod IP",
                "rollback": "无操作，仅查询",
                "risk_level": "low",
                "auto_fixable": False,
            })
        elif fc == "resource_not_ready":
            steps.append({
                "action": (
                    f"检查并恢复 {service} Pod 状态："
                    f" kubectl get pods -n {namespace} -l app={service} -o wide"
                ),
                "verification": "所有 Pod 状态为 Running，READY 列显示 1/1",
                "rollback": (
                    f"如需回滚部署："
                    f" kubectl rollout undo deployment/{service} -n {namespace}"
                ),
                "risk_level": "medium",
                "auto_fixable": False,
                "requires_service_restart": True,
            })
        elif fc in {"empty_output", "timeout"}:
            steps.append({
                "action": (
                    f"扩大查询时间窗口或降低查询复杂度后重新收集 {service} 的诊断数据"
                ),
                "verification": "获取到足量诊断数据",
                "rollback": "无需回滚",
                "risk_level": "low",
                "auto_fixable": False,
            })
        else:
            steps.append({
                "action": (
                    f"人工复查步骤 {step_id!r} 的失败原因并制定针对性修复方案"
                ),
                "verification": "问题症状消失，服务恢复正常",
                "rollback": "按修复操作的具体内容制定回滚方案",
                "risk_level": "medium",
                "auto_fixable": False,
            })

    # If no steps were generated, add a generic investigation step
    if not steps:
        steps.append({
            "action": (
                f"根据以下诊断问题进行人工分析：{run_question[:300]}"
            ),
            "verification": "服务恢复正常且无新告警",
            "rollback": "按具体操作制定",
            "risk_level": "low",
            "auto_fixable": False,
        })

    return steps


# ──────────────────────────────────────────────────────────────────────────────
# Phase 4: Build and save remediation plan
# ──────────────────────────────────────────────────────────────────────────────

def build_and_save_remediation_plan(
    *,
    run: "AgentRun",
    service: "AgentRuntimeService",
) -> Optional[str]:
    """
    Extract a RemediationPlan from a completed diagnostic run and save it to
    the CaseStore as a pending-verification case.

    Returns the new case_id, or None if saving failed or was skipped.
    """
    try:
        from ai.similar_cases import (
            Case,
            RemediationPlan,
            RemediationStep,
            get_case_store,
        )
    except ImportError:
        logger.debug("similar_cases not available; skipping Phase-4 KB save")
        return None

    if not run or not run.run_id:
        return None

    summary = run.summary_json if isinstance(run.summary_json, dict) else {}
    run_question = _as_str(run.question if hasattr(run, "question") else "")
    if not run_question:
        run_question = _as_str(summary.get("question") or summary.get("user_question") or "")

    evidence_summary = _extract_evidence_summary(run)
    service_name = evidence_summary.get("service_name") or ""
    components = evidence_summary.get("components") or []
    failed_steps = evidence_summary.get("failed_steps") or []
    error_keywords = evidence_summary.get("error_keywords") or []
    snippets = evidence_summary.get("snippets") or []

    if not snippets and not failed_steps:
        # Nothing useful to save
        logger.debug(
            "Phase4: no evidence to save for run_id=%s", run.run_id
        )
        return None

    error_category = _infer_error_category(failed_steps)
    remediation_step_dicts = _build_remediation_steps_from_evidence(
        evidence_summary, run_question
    )
    remediation_steps = [RemediationStep(**s) for s in remediation_step_dicts]

    now = _now_iso()
    plan = RemediationPlan(
        plan_id=f"rp-{uuid.uuid4().hex[:16]}",
        case_id="",  # will be set below
        run_id=run.run_id,
        service=service_name,
        error_category=error_category,
        components=components,
        keywords=error_keywords,
        steps=remediation_steps,
        overall_risk="medium" if any(
            s.risk_level == "high" for s in remediation_steps
        ) else "low",
        estimated_total_duration_s=sum(
            s.estimated_duration_s for s in remediation_steps
        ),
        verification_summary="完成所有步骤后验证服务恢复正常，无新错误告警",
        rollback_summary="按各步骤的回滚说明逆序执行",
        created_at=now,
        human_verified=False,
        auto_fix_authorized=False,
    )

    # Build the Case that wraps this plan
    case_id = f"case-auto-{run.run_id[:16]}"
    plan.case_id = case_id

    analysis_summary = "\n".join(snippets[:3]) or "（无成功诊断输出）"

    case = Case(
        id=case_id,
        problem_type=error_category,
        severity="medium",
        summary=run_question[:500] or f"Auto-captured from run {run.run_id}",
        log_content="\n".join(
            _as_str(s.get("snippet")) for s in failed_steps[:3]
        )[:2000],
        service_name=service_name,
        root_causes=[
            _as_str(s.get("failure_category")) for s in failed_steps[:5] if s.get("failure_category")
        ],
        solutions=[{"title": s.action, "steps": [s.verification]} for s in remediation_steps[:3]],
        created_at=now,
        updated_at=now,
        resolved=False,
        source="auto_diagnostic",
        analysis_summary=analysis_summary[:1000],
        manual_remediation_steps=[s.action for s in remediation_steps],
        verification_result="pending_human_verification",
        knowledge_version=1,
        context={
            "run_id": run.run_id,
            "remediation_plan": plan.to_dict(),
            "evidence_summary": {
                "snippet_count": len(snippets),
                "failed_step_count": len(failed_steps),
                "components": components,
            },
        },
        tags=["auto_captured", "pending_verification"] + components[:5],
    )

    try:
        case_store = get_case_store(
            getattr(service, "storage", None)
            or getattr(service, "store", None)
        )
        # Use the store's storage adapter if available
        if hasattr(service, "store") and hasattr(service.store, "storage"):
            case_store = get_case_store(service.store.storage)

        case_store.add_case(case, persist=True, sync_clickhouse=True)
        logger.info(
            "Phase4 KB save: created case %r for run_id=%s service=%r error_category=%r",
            case_id,
            run.run_id,
            service_name,
            error_category,
        )
        return case_id
    except Exception as exc:
        logger.warning(
            "Phase4 KB save failed for run_id=%s: %s",
            run.run_id,
            exc,
            exc_info=True,
        )
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Phase 6: Execute an authorized auto-fix plan
# ──────────────────────────────────────────────────────────────────────────────

async def execute_authorized_auto_fix(
    *,
    case_id: str,
    service: "AgentRuntimeService",
    run_id: str,
    authorized_by: str = "human",
) -> Dict[str, Any]:
    """
    Execute an auto-fix-authorized remediation plan.

    Safety contract:
      1. Plan MUST have ``human_verified=True`` AND ``auto_fix_authorized=True``
      2. Only steps with ``auto_fixable=True`` are executed autonomously
      3. Steps with ``auto_fixable=False`` are logged as "manual_required" and
         returned in the response for human action
      4. All execution is recorded in the case change-history

    Returns a dict with:
      - status: "executed" | "partial" | "manual_required" | "not_authorized"
      - executed_steps: list of {action, result, exit_code}
      - manual_steps: list of {action, reason}
      - case_id
    """
    try:
        from ai.similar_cases import RemediationPlan, get_case_store, mark_case_verified
    except ImportError:
        return {"status": "error", "message": "similar_cases not available"}

    case_store = get_case_store(
        getattr(service, "store", None) and getattr(service.store, "storage", None)
    )
    case = case_store.get_case(case_id)
    if not case:
        return {"status": "not_found", "case_id": case_id}

    plan_dict = (case.context or {}).get("remediation_plan")
    if not isinstance(plan_dict, dict):
        return {"status": "no_plan", "case_id": case_id}

    plan = RemediationPlan.from_dict(plan_dict)

    # Enforce authorization gate
    if not plan.human_verified:
        return {
            "status": "not_authorized",
            "reason": "plan has not been human-verified yet",
            "case_id": case_id,
        }
    if not plan.auto_fix_authorized:
        return {
            "status": "not_authorized",
            "reason": "auto-fix authorization not granted for this plan",
            "case_id": case_id,
        }

    executed: List[Dict[str, Any]] = []
    manual_required: List[Dict[str, Any]] = []

    for step in plan.steps:
        if not step.auto_fixable:
            manual_required.append({
                "action": step.action,
                "reason": "step is not marked auto_fixable; requires manual execution",
                "verification": step.verification,
                "rollback": step.rollback,
            })
            continue

        # Dispatch the auto_fixable step via service.execute_command_tool
        try:
            result = await service.execute_command_tool(
                run_id=run_id,
                action_id=f"autofix-{uuid.uuid4().hex[:8]}",
                tool_call_id=f"autofix-{uuid.uuid4().hex[:8]}",
                command=step.action,
                purpose=f"Phase-6 auto-fix: {step.action[:100]}",
                title=f"[auto-fix] {step.action[:80]}",
                confirmed=True,
                elevated=False,
                confirmation_ticket="",
                timeout_seconds=60,
            )
            exit_code = int((result or {}).get("exit_code") or 0)
            executed.append({
                "action": step.action,
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "verification": step.verification,
            })
        except Exception as exc:
            executed.append({
                "action": step.action,
                "status": "error",
                "error": str(exc),
            })

    # Update the plan's execution metadata
    plan.execution_count += 1
    plan.last_executed_at = _now_iso()
    updated_context = dict(case.context or {})
    updated_context["remediation_plan"] = plan.to_dict()

    # Persist the updated case
    try:
        from ai.similar_cases import Case
        updated_case = Case(**case.to_dict())
        updated_case.context = updated_context
        updated_case.updated_at = _now_iso()
        updated_case.last_editor = authorized_by
        case_store.add_case(updated_case, persist=True, sync_clickhouse=True)
    except Exception as exc:
        logger.warning("Phase6: failed to persist plan execution update: %s", exc)

    all_ok = all(e.get("status") == "completed" for e in executed)
    has_executed = bool(executed)

    return {
        "status": "executed" if (has_executed and not manual_required) else (
            "partial" if has_executed else "manual_required"
        ),
        "all_auto_steps_ok": all_ok,
        "executed_steps": executed,
        "manual_steps": manual_required,
        "case_id": case_id,
        "plan_id": plan.plan_id,
    }
