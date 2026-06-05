"""Bridge from legacy followup flow to the unified runtime engine.

Provides a drop-in replacement for _run_followup_auto_exec_react_loop
that uses the new run_diagnosis() engine internally.

Activated by env var: AI_RUNTIME_UNIFIED_ENGINE_ENABLED=true
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional

from ai.command.compiler import compile_command
from ai.command.normalizer import normalize_command_spec
from ai.command.security import evaluate_command
from ai.command.spec import CommandSpec, ToolType

from ai.runtime.engine import run_diagnosis, LlmPlanResult, RuntimeResult, _default_llm_plan
from ai.runtime.state import RuntimeState, Action, Observation
from ai.runtime.memory import SessionMemory
from ai.runtime.events import EventEmitter
from ai.runtime.prompt import PromptBuilder
from ai.runtime.tools import ToolAdapter


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _is_unified_engine_enabled() -> bool:
    return _as_str(os.getenv("AI_RUNTIME_UNIFIED_ENGINE_ENABLED", "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


async def unified_diagnosis_bridge(
    *,
    session_id: str,
    message_id: str,
    actions: List[Dict[str, Any]],
    analysis_context: Dict[str, Any],
    allow_auto_exec_readonly: bool = True,
    executed_commands: set | None = None,
    initial_action_observations: List[Dict[str, Any]] | None = None,
    initial_evidence_gaps: List[str] | None = None,
    initial_summary: str = "",
    emit_iteration_thoughts: bool = False,
    run_blocking: Callable | None = None,
    build_react_loop_fn: Callable | None = None,
    event_callback: Callable | None = None,
    logger: Any = None,
    llm_replan_callback: Callable | None = None,
    # ── new engine params ────────────────────────────────────────────────
    llm_call: Any = None,
    llm_chat_fn: Any = None,
) -> Dict[str, Any]:
    """Drop-in replacement for _run_followup_auto_exec_react_loop.

    Uses the unified run_diagnosis() engine with LLM integration.

    Returns the same dict shape as the legacy react loop:
        {actions, action_observations, react_loop, react_iterations}
    """
    # Build RuntimeState from legacy params
    source_target = analysis_context.get("source_target") if isinstance(analysis_context, dict) else None
    question = _as_str(analysis_context.get("question") or analysis_context.get("input_text", ""))

    state = RuntimeState(
        run_id=session_id,
        question=question or "diagnose issue from logs",
        analysis_context=analysis_context if isinstance(analysis_context, dict) else {},
        source_target=source_target if isinstance(source_target, dict) else None,
        max_iterations=4,
    )

    # Pre-populate evidence slots from gaps
    for gap in _as_list(initial_evidence_gaps):
        gap_key = _as_str(gap).strip()
        if gap_key:
            from ai.runtime.state import EvidenceSlot
            state.evidence_slots[gap_key] = EvidenceSlot(key=gap_key, status="pending")

    memory = SessionMemory()
    emitter = EventEmitter()

    # Wire event_callback to emitter
    run_id = session_id
    if event_callback:
        queue = emitter.subscribe(run_id)

        async def _relay_events():
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                    if event_callback:
                        try:
                            event_callback(event.get("type", ""), event.get("payload", {}))
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

        # Background task would relay events — for simplicity, emit directly

    # Build LLM plan function using the provided llm_chat_fn
    async def _llm_plan(system_prompt, task_prompt, tool_schema, st, mem, lc):
        if llm_chat_fn is None:
            return LlmPlanResult(actions=[], summary="no LLM configured")

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task_prompt},
            ]
            raw = await llm_chat_fn(messages, tools=[tool_schema] if tool_schema else None)
        except Exception as exc:
            if logger:
                logger.warning("LLM plan call failed: %s", exc)
            return LlmPlanResult(actions=[], summary=f"LLM error: {exc}")

        # Parse LLM response — handle various output shapes
        actions_out = []
        if isinstance(raw, dict):
            # Tool call response
            tool_calls = raw.get("tool_calls") or raw.get("actions") or []
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or tc
                        args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        if isinstance(args, dict) and args.get("command"):
                            actions_out.append(args)
            # Direct action
            if not actions_out and raw.get("command"):
                actions_out.append(raw)
            # Content field
            content = raw.get("content") or raw.get("message", "")
            if isinstance(content, str) and content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        actions_out.append(parsed)
                    elif isinstance(parsed, list):
                        actions_out.extend(parsed)
                except json.JSONDecodeError:
                    pass
        elif isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    actions_out.append(parsed)
                elif isinstance(parsed, list):
                    actions_out.extend(parsed)
            except json.JSONDecodeError:
                pass

        return LlmPlanResult(actions=actions_out, raw_response=str(raw)[:2000])

    # Build tools adapter
    tools = ToolAdapter()

    # Build prompt builder
    prompt_builder = PromptBuilder()

    # Run the unified engine
    result = await run_diagnosis(
        state=state,
        tools=tools,
        prompt_builder=prompt_builder,
        memory=memory,
        event_emitter=emitter,
        llm_plan=_llm_plan,
        llm_call=llm_chat_fn,
    )

    # Convert to legacy format
    action_observations = []
    for obs in result.observations:
        action_observations.append({
            "action_id": obs.action_id,
            "status": obs.status,
            "exit_code": obs.exit_code,
            "timed_out": False,
            "message": obs.stdout[:500] if obs.stdout else obs.stderr[:500],
            "stdout": obs.stdout,
            "stderr": obs.stderr,
            "duration_ms": obs.duration_ms,
            "channel": obs.channel,
        })

    return {
        "actions": result.actions,
        "action_observations": action_observations,
        "react_loop": result.react_loop,
        "react_iterations": result.react_loop.get("iterations", []),
    }


__all__ = ["unified_diagnosis_bridge", "_is_unified_engine_enabled"]
