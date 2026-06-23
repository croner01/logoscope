"""诊断上下文构建 — 从 _run_follow_up_analysis_core 前段纯提取。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from api.ai import (
    AI_FOLLOWUP_REACT_MEMORY_HITS_TOTAL,
    _as_float,
    _as_list,
    _as_str,
    _build_context_pills,
    _build_followup_actions,
    _build_followup_fallback_answer,
    _build_followup_history,
    _build_followup_long_term_memory,
    _build_followup_planner_prompt,
    _build_followup_react_memory,
    _build_followup_references,
    _build_followup_reflection,
    _build_followup_response_instruction,
    _build_followup_runtime_thread_memory,
    _build_followup_subgoals,
    _build_llm_replan_context,
    _build_llm_replan_context,
    _clear_conversation_history,
    _compact_conversation_for_prompt,
    _emit_followup_event,
    _ensure_followup_analysis_session,
    _estimate_token_usage,
    _extract_overview_summary,
    _extract_success_commands_from_assistant_message,
    _find_duplicate_question_turn,
    _get_conversation_history,
    _is_ai_runtime_lab_mode,
    _is_llm_configured,
    _mask_sensitive_payload,
    _mask_sensitive_text,
    _merge_conversation_history,
    _merge_reflection_with_react_memory,
    _metric_inc,
    _normalize_conversation_history,
    _prioritize_followup_actions_with_react_memory,
    _remaining_timeout,
    _resolve_followup_answer_bundle,
    _resolve_followup_engine,
    _resolve_followup_timeout_profile,
    _run_blocking,
    _session_messages_to_conversation_history,
    _trim_conversation_history,
    _upsert_followup_user_message,
    _utc_now_iso,
    get_agent_runtime_service,
    get_llm_service,
    logger,
    normalize_command_line,
    run_followup_langchain,
)


@dataclass
class DiagnosisContext:
    """诊断上下文 — 包含会话、历史、记忆、推理产物。"""

    # ── 会话标识 ──
    session_id: str
    conversation_id: str
    source_target: Optional[Dict[str, Any]]

    # ── 问题和上下文 ──
    question: str
    analysis_context: Dict[str, Any]

    # ── 历史 ──
    history: List[Dict[str, Any]]
    compacted_summary: str

    # ── 记忆 ──
    long_term_memory: Dict[str, Any]
    react_memory: Dict[str, Any]
    runtime_thread_memory: Dict[str, Any]

    # ── 推理产物 ──
    subgoals: List[Dict[str, Any]]
    reflection: Dict[str, Any]
    planner_prompt: str

    # ── 动作 ──
    followup_actions: List[Dict[str, Any]]
    executed_commands_set: Set[str]
    prior_action_observations: List[Dict[str, Any]]
    evidence_gap_queue_for_execution: List[str]
    answer_summary_seed: str

    # ── LLM ──
    llm_enabled: bool
    llm_requested: bool
    token_budget: int
    token_estimation: int
    followup_engine: str

    # ── 运行时 ──
    timeout_profile: Dict[str, Any]
    deadline_ts: float
    show_thought: bool

    # ── 回调 ──
    event_callback: Optional[Callable]
    run_blocking: Callable


async def build_diagnosis_context(
    request: Any,
    session_store: Any,
    *,
    storage: Any,
    llm_service: Any,
) -> DiagnosisContext:
    """执行所有共享前置逻辑，返回 DiagnosisContext。

    这是 _run_follow_up_analysis_core 第 6491-7047 行的纯提取。
    不执行任何命令，不产生副作用（除创建/查询 session）。

    参数与 _run_follow_up_analysis_core 完全兼容：
    - request: FollowUpRequest 对象
    - session_store: AI 会话存储
    - storage: 通用存储
    - llm_service: LLM 服务
    """
    # ── 输入验证 + 脱敏 ──
    question = _as_str(request.question)
    if not question:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="question is required")

    timeout_profile = _resolve_followup_timeout_profile()
    deadline_ts = time.perf_counter() + float(timeout_profile["request_deadline_seconds"])
    safe_question = _mask_sensitive_text(question)
    analysis_context = _mask_sensitive_payload(request.analysis_context or {})
    if safe_question and not analysis_context.get("question"):
        analysis_context["question"] = safe_question
    runtime_lab_mode = _is_ai_runtime_lab_mode(analysis_context=analysis_context)
    show_thought = bool(getattr(request, "show_thought", False))
    thought_timeline: List[Dict[str, Any]] = []
    event_callback = getattr(request, "event_callback", None)

    # ── api/ai.py:6512-7047 逐行复制 — 从 _emit_thought 到 _llm_replan_callback ──

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
        if duplicate_question_turn:
            assistant_message = (
                duplicate_question_turn.get("assistant_message")
                if isinstance(duplicate_question_turn.get("assistant_message"), dict)
                else {}
            )
            dedupe_commands = _extract_success_commands_from_assistant_message(assistant_message)
            if dedupe_commands:
                existing_runtime_commands = [
                    normalize_command_line(item)
                    for item in _as_list(analysis_context.get("_runtime_executed_commands"))
                    if normalize_command_line(item)
                ]
                merged_runtime_commands = existing_runtime_commands[:]
                for command in dedupe_commands:
                    if command not in merged_runtime_commands:
                        merged_runtime_commands.append(command)
                analysis_context["_runtime_executed_commands"] = merged_runtime_commands[-300:]

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
        analysis_context=analysis_context,
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
        normalize_command_line(item)
        for item in _as_list(analysis_context.get("_runtime_executed_commands"))
        if normalize_command_line(item)
    }
    prior_action_observations = [
        item
        for item in _as_list(analysis_context.get("_runtime_prior_action_observations"))
        if isinstance(item, dict)
    ]

    _replan_llm_timeout = max(10, int(float(timeout_profile["llm_total_timeout_seconds"]) * 0.6))

    async def _llm_replan_callback(
        *,
        original_question: str,
        analysis_context: Optional[Dict[str, Any]],
        all_observations: List[Dict[str, Any]],
        executed_commands: set[str],
        current_evidence_gaps: List[str],
        remaining_iterations: int,
        remaining_timeout: float,
        event_callback: Optional[Any] = None,
        logger: Optional[Any] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """LLM 重规划回调：使用执行上下文调用大模型生成新动作。"""
        replan_context = _build_llm_replan_context(
            original_question=original_question,
            analysis_context=analysis_context,
            all_observations=all_observations,
            executed_commands=executed_commands,
            current_evidence_gaps=current_evidence_gaps,
            remaining_iterations=remaining_iterations,
            remaining_timeout=remaining_timeout,
        )
        try:
            replan_llm_service = get_llm_service()
        except Exception as exc:
            logger and logger.warning("LLM replan: failed to get llm service: %s", exc)
            return None

        # ── 首次调用 ──────────────────────────────────────────────────────────
        augmented_context = dict(analysis_context) if analysis_context else {}
        augmented_context["_llm_replan_context"] = replan_context
        try:
            replan_bundle = await asyncio.wait_for(
                run_followup_langchain(
                    question=f"[重规划] {original_question[:200]}",
                    analysis_context=augmented_context,
                    compacted_history=[],
                    compacted_summary="",
                    references=[],
                    subgoals=[],
                    reflection={},
                    long_term_memory={"enabled": False, "hits": 0, "summary": "", "items": []},
                    llm_enabled=llm_enabled,
                    llm_requested=True,
                    token_budget=min(token_budget, 4000),
                    token_warning=False,
                    llm_timeout_seconds=_replan_llm_timeout,
                    llm_first_token_timeout_seconds=20,
                    llm_service=replan_llm_service,
                    fallback_builder=lambda *args, **kwargs: _build_followup_fallback_answer(*args, **kwargs),
                    stream_token_callback=None,
                ),
                timeout=_replan_llm_timeout,
            )
        except asyncio.TimeoutError:
            logger and logger.warning("LLM replan timed out after %ss", _replan_llm_timeout)
            return None
        except Exception as exc:
            logger and logger.warning("LLM replan failed: %s", exc)
            return None

        new_actions_raw = _as_list(replan_bundle.get("langchain_actions"))
        if new_actions_raw:
            return new_actions_raw

        # ── 重试：空动作时附带反馈再次调用 ────────────────────────────────────
        logger and logger.warning(
            "LLM replan returned empty actions (question=%s), retrying with feedback",
            original_question[:80],
        )
        retry_context = replan_context + (
            "\n\n【反馈】\n"
            "上一轮你返回了空动作列表。请基于已执行命令的失败信息，"
            "生成具体的下一步诊断命令（ClickHouse 查询或 kubectl 命令）。"
            "不要输出空列表。"
        )
        augmented_context_retry = dict(analysis_context) if analysis_context else {}
        augmented_context_retry["_llm_replan_context"] = retry_context
        _retry_timeout = min(_replan_llm_timeout, 25)
        try:
            replan_bundle_retry = await asyncio.wait_for(
                run_followup_langchain(
                    question=f"[重规划] {original_question[:200]}",
                    analysis_context=augmented_context_retry,
                    compacted_history=[],
                    compacted_summary="",
                    references=[],
                    subgoals=[],
                    reflection={},
                    long_term_memory={"enabled": False, "hits": 0, "summary": "", "items": []},
                    llm_enabled=llm_enabled,
                    llm_requested=True,
                    token_budget=min(token_budget, 4000),
                    token_warning=False,
                    llm_timeout_seconds=_retry_timeout,
                    llm_first_token_timeout_seconds=15,
                    llm_service=replan_llm_service,
                    fallback_builder=lambda *args, **kwargs: _build_followup_fallback_answer(*args, **kwargs),
                    stream_token_callback=None,
                ),
                timeout=_retry_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            logger and logger.warning("LLM replan retry also failed")
            return None

        new_actions_retry = _as_list(replan_bundle_retry.get("langchain_actions"))
        if not new_actions_retry:
            logger and logger.warning("LLM replan retry also returned empty actions")
            return None
        return new_actions_retry

    # ── 返回上下文 ──
    return DiagnosisContext(
        session_id=analysis_session_id,
        conversation_id=conversation_id,
        source_target=getattr(request, "source_target", None),
        question=safe_question,
        analysis_context=analysis_context,
        history=history,
        compacted_summary=compacted_summary,
        long_term_memory=long_term_memory,
        react_memory=react_memory,
        runtime_thread_memory=runtime_thread_memory,
        subgoals=subgoals,
        reflection=reflection,
        planner_prompt=planner_prompt,
        followup_actions=followup_actions,
        executed_commands_set=executed_commands_set,
        prior_action_observations=prior_action_observations,
        evidence_gap_queue_for_execution=evidence_gap_queue_for_execution,
        answer_summary_seed=answer_summary_seed,
        llm_enabled=llm_enabled,
        llm_requested=llm_requested,
        token_budget=token_budget,
        token_estimation=token_estimate,
        followup_engine=followup_engine,
        timeout_profile=timeout_profile,
        deadline_ts=deadline_ts,
        show_thought=show_thought,
        event_callback=event_callback,
        run_blocking=getattr(request, "run_blocking", None),
    )
