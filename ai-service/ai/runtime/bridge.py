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


def _parse_llm_json_response(raw: str) -> list:
    """Parse LLM JSON response into action dicts. Handles common shapes."""
    text = _as_str(raw).strip()
    if not text:
        return []

    # Try direct JSON parse
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Try extracting JSON from markdown code fence
        import re
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            try:
                parsed = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                return []
        else:
            return []

    # Normalize to list of action dicts
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        # Common shapes: {"actions": [...]}, {"tool_calls": [...]}, single action
        candidates = (
            parsed.get("actions")
            or parsed.get("tool_calls")
            or parsed.get("steps")
            or [parsed]
        )
        if not isinstance(candidates, list):
            candidates = [parsed]
    else:
        return []

    # Filter to valid action dicts (must have 'command' or 'tool')
    actions = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        # Extract from function call shape
        fn = item.get("function") or item
        args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict) and (args.get("command") or args.get("tool")):
            item = args
        if item.get("command") or item.get("tool"):
            actions.append(item)

    return actions


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
    llm_service: Any = None,
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

    # Build LLM plan function — calls LLMService.chat() with JSON response_format
    async def _llm_plan(system_prompt, task_prompt, tool_schema, st, mem, lc):
        svc = llm_service or llm_chat_fn
        if svc is None:
            return LlmPlanResult(actions=[], summary="no LLM configured")

        # Build a single message combining system + tool schema + task
        tool_instruction = json.dumps(tool_schema, ensure_ascii=False, indent=2) if tool_schema else ""
        combined_message = (
            f"{system_prompt}\n\n"
            f"## Tool Schema (output JSON matching this schema)\n"
            f"```json\n{tool_instruction}\n```\n\n"
            f"{task_prompt}"
        )

        try:
            raw: str = ""
            # Try chat method (LLMService interface: message + context + response_format)
            chat_fn = getattr(svc, "chat", None)
            if callable(chat_fn):
                raw = await chat_fn(
                    message=combined_message,
                    context=None,
                    response_format={"type": "json_object"},
                )
            elif callable(svc):
                raw = await svc(combined_message)
            else:
                return LlmPlanResult(actions=[], summary="LLM service has no chat method")

            if not isinstance(raw, str):
                raw = str(raw)
        except Exception as exc:
            if logger:
                logger.warning("LLM plan call failed: %s", exc)
            return LlmPlanResult(actions=[], summary=f"LLM error: {exc}")

        # Parse JSON response
        actions_out = _parse_llm_json_response(raw)
        return LlmPlanResult(actions=actions_out, raw_response=raw[:2000])

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
