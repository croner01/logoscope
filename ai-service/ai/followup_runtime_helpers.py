"""
Follow-up runtime helpers.

Move heavyweight follow-up runtime logic out of `api/ai.py` while keeping
behavior stable through explicit dependency injection.
"""

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional

from ai.llm_stream_helpers import collect_chat_response


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


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _run_field(run: Any, key: str) -> Any:
    if isinstance(run, dict):
        return run.get(key)
    return getattr(run, key, None)


def _build_followup_runtime_thread_memory(
    runtime_runs: List[Any],
    *,
    max_runs: int = 4,
    max_items: int = 4,
) -> Dict[str, Any]:
    """从最近 runtime run 摘要里提炼线程记忆。"""
    facts: List[str] = []
    failed_actions: List[str] = []
    successful_actions: List[str] = []
    approval_history: List[str] = []
    user_constraints: List[str] = []

    seen_facts = set()
    seen_failed = set()
    seen_success = set()
    seen_approvals = set()
    seen_constraints = set()

    for run in _as_list(runtime_runs)[: max(1, int(max_runs or 4))]:
        summary = _as_dict(_run_field(run, "summary_json"))
        question = _as_str((_as_dict(_run_field(run, "input_json"))).get("question")).strip()
        command = _as_str(summary.get("last_command")).strip()
        purpose = _as_str(summary.get("last_command_purpose")).strip() or question
        status = _as_str(summary.get("last_command_status")).strip().lower()
        error_detail = _as_str(summary.get("last_command_error_detail")).strip()

        if status in {"failed", "timed_out", "blocked"}:
            line = f"{purpose or command or '上一轮动作'} 状态={status}"
            if error_detail:
                line = f"{line}，原因={error_detail[:120]}"
            if line not in seen_failed:
                seen_failed.add(line)
                failed_actions.append(line)
        elif status == "completed":
            line = f"{purpose or command or '上一轮动作'} 已完成"
            if line not in seen_success:
                seen_success.add(line)
                successful_actions.append(line)

        recovery_kind = _as_str(summary.get("last_recovery_kind")).strip()
        if recovery_kind:
            line = f"系统最近一次内部恢复策略：{recovery_kind}"
            if line not in seen_facts:
                seen_facts.add(line)
                facts.append(line)

        timeout_variant = _as_dict(summary.get("last_timeout_recovery_variant"))
        timeout_message = _as_str(timeout_variant.get("message")).strip()
        if timeout_message:
            line = f"超时后系统已尝试：{timeout_message}"
            if line not in seen_facts:
                seen_facts.add(line)
                facts.append(line)

        last_approval = _as_dict(summary.get("last_approval"))
        approval_id = _as_str(last_approval.get("approval_id")).strip()
        approval_decision = _as_str(last_approval.get("decision")).strip()
        if approval_id and approval_decision:
            line = f"审批 {approval_id} = {approval_decision}"
            if line not in seen_approvals:
                seen_approvals.add(line)
                approval_history.append(line)

        last_user_input = _as_dict(summary.get("last_user_input"))
        business_answer = _as_str(
            last_user_input.get("business_answer_text") or last_user_input.get("text")
        ).strip()
        if business_answer:
            line = business_answer[:160]
            if line not in seen_constraints:
                seen_constraints.add(line)
                user_constraints.append(line)

        if len(facts) >= max_items and len(failed_actions) >= max_items and len(user_constraints) >= max_items:
            break

    summary_lines: List[str] = []
    if facts:
        summary_lines.append(f"已知事实：{'；'.join(facts[:max_items])}")
    if failed_actions:
        summary_lines.append(f"最近失败动作：{'；'.join(failed_actions[:max_items])}")
    if successful_actions:
        summary_lines.append(f"最近成功动作：{'；'.join(successful_actions[:max_items])}")
    if approval_history:
        summary_lines.append(f"最近审批结果：{'；'.join(approval_history[:max_items])}")
    if user_constraints:
        summary_lines.append(f"用户刚确认的约束：{'；'.join(user_constraints[:max_items])}")

    hits = sum(
        1
        for bucket in (facts, failed_actions, successful_actions, approval_history, user_constraints)
        if bucket
    )
    return {
        "enabled": True,
        "hits": hits,
        "summary": "\n".join(summary_lines)[:2200],
        "facts": facts[:max_items],
        "failed_actions": failed_actions[:max_items],
        "successful_actions": successful_actions[:max_items],
        "approval_history": approval_history[:max_items],
        "user_constraints": user_constraints[:max_items],
    }


async def _load_messages_for_history(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    session_id: str,
    limit: int,
) -> List[Any]:
    """优先读取轻量消息，避免长期记忆扫描时回读 metadata_json。"""
    light_getter = getattr(session_store, "get_messages_light", None)
    if callable(light_getter):
        return await run_blocking(light_getter, session_id, limit)
    return await run_blocking(session_store.get_messages, session_id, limit)


async def _build_followup_long_term_memory(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    analysis_context: Dict[str, Any],
    question: str,
) -> Dict[str, Any]:
    """构建跨会话长期记忆摘要（P1）。"""
    enabled = _as_str(os.getenv("AI_FOLLOWUP_LONG_TERM_MEMORY_ENABLED"), "true").lower() == "true"
    if not enabled:
        return {"enabled": False, "hits": 0, "summary": "", "items": []}

    session_limit = max(1, int(_as_float(os.getenv("AI_FOLLOWUP_LONG_TERM_MEMORY_SESSION_LIMIT", 6), 6)))
    max_snippets = max(1, int(_as_float(os.getenv("AI_FOLLOWUP_LONG_TERM_MEMORY_MAX_SNIPPETS", 6), 6)))
    message_limit = max(2, int(_as_float(os.getenv("AI_FOLLOWUP_LONG_TERM_MEMORY_MESSAGE_LIMIT", 4), 4)))

    service_name = _as_str(analysis_context.get("service_name"))
    trace_id = _as_str(analysis_context.get("trace_id"))
    request_id = _as_str(analysis_context.get("request_id"))
    keyword_hint = _as_str(question)[:60]

    candidate_sessions: List[Any] = []
    if service_name:
        by_service = await run_blocking(
            session_store.list_sessions,
            limit=session_limit * 2,
            offset=0,
            analysis_type="log",
            service_name=service_name,
            include_archived=False,
            search_query="",
            pinned_first=True,
            sort_by="updated_at",
            sort_order="desc",
        )
        candidate_sessions.extend(_as_list(by_service))

    for search_query in [trace_id, request_id, keyword_hint]:
        search_text = _as_str(search_query)
        if not search_text:
            continue
        by_search = await run_blocking(
            session_store.list_sessions,
            limit=session_limit,
            offset=0,
            analysis_type="",
            service_name="",
            include_archived=False,
            search_query=search_text,
            pinned_first=True,
            sort_by="updated_at",
            sort_order="desc",
        )
        candidate_sessions.extend(_as_list(by_search))

    deduped: List[Any] = []
    seen_session_ids = set()
    for session in candidate_sessions:
        sid = _as_str(getattr(session, "session_id", ""))
        if not sid or sid == analysis_session_id or sid in seen_session_ids:
            continue
        seen_session_ids.add(sid)
        deduped.append(session)
        if len(deduped) >= session_limit:
            break

    memory_items: List[Dict[str, str]] = []
    for session in deduped[:session_limit]:
        sid = _as_str(getattr(session, "session_id", ""))
        title = _as_str(getattr(session, "title", ""))
        summary = _as_str(
            getattr(session, "summary_text", "")
            or title
            or getattr(session, "input_text", "")
        )[:220]
        assistant_hint = ""
        if sid:
            messages = await _load_messages_for_history(
                session_store=session_store,
                run_blocking=run_blocking,
                session_id=sid,
                limit=message_limit,
            )
            for msg in reversed(_as_list(messages)):
                role = _as_str(getattr(msg, "role", ""))
                content = _as_str(getattr(msg, "content", ""))
                if role == "assistant" and content:
                    assistant_hint = content[:220]
                    break
        memory_items.append(
            {
                "session_id": sid,
                "service_name": _as_str(getattr(session, "service_name", "")),
                "trace_id": _as_str(getattr(session, "trace_id", "")),
                "updated_at": _as_str(getattr(session, "updated_at", "")),
                "title": title,
                "summary": summary,
                "assistant_hint": assistant_hint,
            }
        )
        if len(memory_items) >= max_snippets:
            break

    summary_lines: List[str] = []
    for item in memory_items:
        line_parts = [
            f"session={_as_str(item.get('session_id'))}",
            f"service={_as_str(item.get('service_name')) or 'unknown'}",
            f"trace={_as_str(item.get('trace_id')) or 'N/A'}",
            f"summary={_as_str(item.get('summary'))}",
        ]
        assistant_hint = _as_str(item.get("assistant_hint"))
        if assistant_hint:
            line_parts.append(f"assistant_hint={assistant_hint}")
        summary_lines.append(" | ".join(line_parts))

    return {
        "enabled": True,
        "hits": len(memory_items),
        "summary": "\n".join(summary_lines[:max_snippets])[:2200],
        "items": memory_items[:max_snippets],
    }


async def _resolve_followup_answer_bundle(
    *,
    safe_question: str,
    analysis_context: Dict[str, Any],
    compacted_history: List[Dict[str, Any]],
    compacted_summary: str,
    references: List[Dict[str, str]],
    subgoals: List[Dict[str, Any]],
    reflection: Dict[str, Any],
    planner_prompt: str,
    long_term_memory: Dict[str, Any],
    llm_enabled: bool,
    llm_requested: bool,
    token_budget: int,
    token_warning: bool,
    llm_timeout_seconds: int,
    analysis_session_id: str,
    llm_first_token_timeout_seconds: int = 20,
    resolve_followup_engine: Callable[[], str],
    run_followup_langchain_fn: Callable[..., Any],
    get_llm_service_fn: Callable[[], Any],
    build_followup_fallback_answer: Callable[..., str],
    build_followup_response_instruction: Callable[..., str],
    stream_token_callback: Optional[Callable[[str], Any]] = None,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    llm_timeout_fallback = False
    followup_engine = resolve_followup_engine()
    langchain_actions: List[Dict[str, Any]] = []
    missing_evidence: List[str] = []
    analysis_summary = ""

    if followup_engine == "langchain":
        langchain_result = await run_followup_langchain_fn(
            question=safe_question,
            analysis_context=analysis_context,
            compacted_history=compacted_history,
            compacted_summary=compacted_summary,
            references=references,
            subgoals=subgoals,
            reflection=reflection,
            long_term_memory=long_term_memory,
            llm_enabled=llm_enabled,
            llm_requested=llm_requested,
            token_budget=token_budget,
            token_warning=token_warning,
            llm_timeout_seconds=llm_timeout_seconds,
            llm_first_token_timeout_seconds=llm_first_token_timeout_seconds,
            llm_service=get_llm_service_fn() if (llm_enabled and llm_requested) else None,
            fallback_builder=build_followup_fallback_answer,
            stream_token_callback=stream_token_callback,
        )
        answer = _as_str(langchain_result.get("answer"))
        method = _as_str(langchain_result.get("analysis_method"), "rule-based")
        llm_timeout_fallback = bool(langchain_result.get("llm_timeout_fallback"))
        langchain_actions = _as_list(langchain_result.get("actions"))
        missing_evidence = [
            _as_str(item).strip()
            for item in _as_list(langchain_result.get("missing_evidence"))
            if _as_str(item).strip()
        ]
        analysis_summary = _as_str(langchain_result.get("analysis_summary")).strip()[:280]
    elif llm_enabled and llm_requested:
        llm_service = get_llm_service_fn()
        prompt = safe_question
        response_instruction = build_followup_response_instruction(
            has_references=bool(references),
            token_warning=token_warning,
        )
        if references:
            ref_text = "\n".join(
                [f"[{ref.get('id')}] {ref.get('title')}: {ref.get('snippet')}" for ref in references]
            )
            prompt = (
                f"{safe_question}\n\n"
                f"{response_instruction}\n\n"
                "证据片段：\n"
                f"{ref_text}\n\n"
                f"{planner_prompt}"
            )
        else:
            prompt = (
                f"{safe_question}\n\n"
                f"{response_instruction}\n"
                f"{planner_prompt}"
            )
        try:
            answer = await collect_chat_response(
                llm_service=llm_service,
                message=prompt,
                context={
                    "analysis_context": analysis_context,
                    "conversation_history": compacted_history[-10:],
                    "conversation_summary": compacted_summary,
                    "references": references,
                    "subgoals": subgoals,
                    "reflection": reflection,
                    "long_term_memory": long_term_memory,
                    "token_budget": token_budget,
                },
                total_timeout_seconds=llm_timeout_seconds,
                first_token_timeout_seconds=llm_first_token_timeout_seconds,
                on_token=stream_token_callback,
            )
            if _as_str(answer):
                method = "llm"
            else:
                if logger is not None:
                    logger.warning(
                        "AI follow-up LLM returned empty answer, fallback to rule-based "
                        f"(session_id={analysis_session_id})"
                    )
                answer = build_followup_fallback_answer(
                    safe_question,
                    analysis_context,
                    fallback_reason="llm_unavailable",
                    reflection=reflection,
                )
                method = "rule-based"
        except asyncio.TimeoutError:
            if logger is not None:
                logger.warning(
                    "AI follow-up LLM timeout, fallback to rule-based answer "
                    f"(timeout={llm_timeout_seconds}s, session_id={analysis_session_id})"
                )
            answer = build_followup_fallback_answer(
                safe_question,
                analysis_context,
                fallback_reason="llm_timeout",
                reflection=reflection,
            )
            method = "rule-based"
            llm_timeout_fallback = True
        except Exception as exc:
            error_text = _as_str(exc).lower()
            is_timeout_error = (
                isinstance(exc, TimeoutError)
                or "timeout" in error_text
                or "timed out" in error_text
                or "deadline exceeded" in error_text
            )
            if logger is not None:
                logger.warning(
                    "AI follow-up LLM error, fallback to rule-based answer "
                    f"(session_id={analysis_session_id}, timeout_like={is_timeout_error}): {exc}"
                )
            answer = build_followup_fallback_answer(
                safe_question,
                analysis_context,
                fallback_reason="llm_timeout" if is_timeout_error else "llm_unavailable",
                reflection=reflection,
            )
            method = "rule-based"
            llm_timeout_fallback = bool(is_timeout_error)
    else:
        fallback_reason = "llm_unavailable"
        if llm_enabled and not llm_requested:
            fallback_reason = "llm_disabled_by_user"
        answer = build_followup_fallback_answer(
            safe_question,
            analysis_context,
            fallback_reason=fallback_reason,
            reflection=reflection,
        )
        method = "rule-based"
    if not analysis_summary:
        analysis_summary = _as_str(answer).strip().splitlines()[0][:280] if _as_str(answer).strip() else ""

    return {
        "answer": answer,
        "analysis_method": method,
        "llm_timeout_fallback": llm_timeout_fallback,
        "followup_engine": followup_engine,
        "langchain_actions": langchain_actions,
        "missing_evidence": missing_evidence,
        "analysis_summary": analysis_summary,
    }
