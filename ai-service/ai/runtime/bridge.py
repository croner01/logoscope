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
    """Parse LLM JSON response into action dicts. Handles common shapes.

    Covers:
    - Direct action: {"tool": "...", "command": "..."}
    - Array: [{"tool": "...", "command": "..."}]
    - Wrapped: {"actions": [...]}
    - Function call: {"tool_calls": [{"function": {"arguments": {...}}}]}
    - DeepSeek json_object: top-level dict with embedded action fields
    - Markdown code fence: ```json {...} ```
    - Plain text with embedded JSON (last-resort extraction)
    """
    text = _as_str(raw).strip()
    if not text:
        return []

    # ── Layer 3: json_repair — auto-fix common syntax errors ──────────
    # Handles single quotes, trailing commas, missing brackets, and
    # extra text around the JSON object — before attempting json.loads().
    try:
        from json_repair import repair_json
        repaired = repair_json(text)
        parsed = json.loads(repaired)
    except Exception:
        # json_repair failed → fall through to direct parse + extraction chain
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
                    # Last resort: try to find any JSON object in the text
                    m2 = re.search(r'\{[^{}]*"(?:tool|command|purpose)"[^{}]*\}', text)
                    if m2:
                        try:
                            parsed = json.loads(m2.group(0))
                        except json.JSONDecodeError:
                            parsed = None
                    else:
                        parsed = None
            else:
                # Last resort: try to find any JSON object in the text
                m2 = re.search(r'\{[^{}]*"(?:tool|command|purpose)"[^{}]*\}', text)
                if m2:
                    try:
                        parsed = json.loads(m2.group(0))
                    except json.JSONDecodeError:
                        parsed = None
                else:
                    parsed = None

            if parsed is None:
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
            or parsed.get("diagnostic_actions")
            or None
        )
        if candidates is None:
            # Check if the top-level dict is itself an action (has command/tool)
            if parsed.get("command") or parsed.get("tool"):
                candidates = [parsed]
            else:
                # DeepSeek json_object may wrap in "response" or root key
                for key in ("response", "result", "action", "data"):
                    inner = parsed.get(key)
                    if isinstance(inner, dict):
                        if inner.get("command") or inner.get("tool") or inner.get("actions"):
                            parsed = inner
                            break
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
        # Extract from function call shape: {"function": {"arguments": {...}}}
        fn = item.get("function") or item
        args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if isinstance(args, dict) and (args.get("command") or args.get("tool")):
            item = args
        # Accept items with command or tool fields
        if item.get("command") or item.get("tool"):
            # Ensure required fields have defaults
            if "tool" not in item:
                # Auto-detect: SQL → clickhouse_query, shell → generic_exec
                cmd = _as_str(item.get("command", "")).strip().upper()
                item["tool"] = "clickhouse_query" if cmd.startswith("SELECT") else "generic_exec"
            if "purpose" not in item:
                item["purpose"] = item.get("description", "diagnostic action")
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

    # Wire event_callback to emitter via background relay task
    if event_callback:
        queue = emitter.subscribe(session_id)

        # Detect whether the callback is async so we can await it properly
        _is_async_callback = asyncio.iscoroutinefunction(event_callback)

        async def _relay_events():
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    try:
                        if _is_async_callback:
                            await event_callback(event.get("type", ""), event.get("payload", {}))
                        else:
                            event_callback(event.get("type", ""), event.get("payload", {}))
                    except Exception:
                        pass
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

        # Start relay in background (non-blocking)
        asyncio.create_task(_relay_events())

    async def _try_llm_chat(svc: Any, message: str, use_json_format: bool, log_fn: Any) -> str:
        """Call LLMService.chat() and return the response string."""
        chat_fn = getattr(svc, "chat", None)
        if not callable(chat_fn):
            raise RuntimeError("LLM service has no chat method")

        kwargs: dict = {"message": message, "context": None}
        if use_json_format:
            kwargs["response_format"] = {"type": "json_object"}
            # Pre-filling: force the model to start output from the correct key.
            # The provider appends this as an assistant message so the model
            # continues from `{"actions":[` instead of inventing an outer wrapper.
            kwargs["assistant_prefix"] = '{"actions":['

        try:
            raw = await chat_fn(**kwargs)
            if not isinstance(raw, str):
                raw = str(raw)
            return raw
        except Exception as exc:
            if log_fn:
                log_fn.warning("bridge: session=%s LLM chat call failed: %s", session_id, exc)
            raise

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

        provider = _as_str(getattr(getattr(svc, "config", None), "provider", "")).lower()
        supports_json_format = provider in ("deepseek", "openai", "local")

        # Try primary call with response_format (includes pre-filling)
        raw = await _try_llm_chat(svc, combined_message, supports_json_format, logger)
        actions_out = _parse_llm_json_response(raw)

        # If no actions parsed, retry with error feedback in the prompt.
        # The model sees what it produced and is told exactly what was wrong.
        if not actions_out and supports_json_format:
            raw_preview = _as_str(raw)[:300]
            if logger:
                logger.warning(
                    "bridge: session=%s no actions parsed from json_object response (len=%d), retrying with error feedback. "
                    "raw_preview=%s",
                    session_id, len(raw), raw_preview,
                )
            # Layer 4: feedback retry — tell the model what failed
            feedback = (
                f"\n\n## ⚠️ PARSE ERROR — YOUR PREVIOUS OUTPUT WAS REJECTED\n"
                f"Your last response could not be parsed as valid JSON. It was:\n"
                f"```\n{raw_preview}\n```\n"
                f"Please output ONLY a valid JSON object with an \"actions\" array.\n"
                f'Example: {{"actions":[{{"tool":"clickhouse_query","command":"SELECT 1","purpose":"test"}}]}}\n'
                f"Use double quotes. No trailing commas. No analysis text."
            )
            raw2 = await _try_llm_chat(svc, combined_message + feedback, False, logger)
            actions_out2 = _parse_llm_json_response(raw2)
            if actions_out2:
                raw = raw2
                actions_out = actions_out2
                if logger:
                    logger.info(
                        "bridge: session=%s feedback retry succeeded, %d actions",
                        session_id, len(actions_out),
                    )

        # Layer 4: degradation fallback — if structured parsing failed,
        # try to extract any SELECT or shell command from the raw text
        if not actions_out:
            import re
            fallback_actions = []
            # Look for SQL statements
            for m in re.finditer(
                r"(SELECT\s+.*?FROM\s+\S+(?:\s+WHERE\s+.*?)?(?:LIMIT\s+\d+)?)",
                _as_str(raw), re.IGNORECASE | re.DOTALL,
            ):
                sql = m.group(1).strip()[:500]
                if sql:
                    fallback_actions.append({
                        "tool": "clickhouse_query",
                        "command": sql,
                        "target_kind": "clickhouse_cluster",
                        "target_identity": "database:logs",
                        "purpose": "extracted from unstructured LLM response",
                    })
            if fallback_actions:
                actions_out = fallback_actions
                if logger:
                    logger.warning(
                        "bridge: session=%s degraded fallback extracted %d actions from raw text",
                        session_id, len(actions_out),
                    )

        # Log outcome
        if logger:
            if actions_out:
                logger.info(
                    "bridge: session=%s LLM plan generated %d actions: %s",
                    session_id,
                    len(actions_out),
                    [(a.get("tool", ""), _as_str(a.get("command", ""))[:80]) for a in actions_out],
                )
            else:
                logger.warning(
                    "bridge: session=%s LLM plan returned no parseable actions. raw_len=%d raw_preview=%s",
                    session_id, len(raw), _as_str(raw)[:300],
                )

        return LlmPlanResult(actions=actions_out, raw_response=raw[:2000])

    # Build tools adapter
    tools = ToolAdapter()

    # Build prompt builder
    prompt_builder = PromptBuilder()

    # Wire legacy replan callback as engine's on_iteration hook
    async def _on_iteration(iteration: int, st: RuntimeState, mem: SessionMemory) -> None:
        if llm_replan_callback and callable(llm_replan_callback):
            try:
                # Build a minimal replan context from observations
                replan_context = {
                    "iteration": iteration,
                    "observations": [
                        {"action_id": o.action_id, "status": o.status, "exit_code": o.exit_code,
                         "stdout": o.stdout[:500], "stderr": o.stderr[:200]}
                        for o in st.observations[-5:]
                    ],
                    "evidence_slots": {
                        k: s.status for k, s in st.evidence_slots.items()
                    },
                }
                new_actions = await llm_replan_callback(replan_context)
                if new_actions and isinstance(new_actions, list):
                    for i, raw in enumerate(new_actions):
                        if isinstance(raw, dict):
                            try:
                                spec = normalize_command_spec(raw, source_target=st.source_target)
                                act = Action(
                                    action_id=f"replan{iteration}-act{i}",
                                    command_spec=spec,
                                    purpose=spec.purpose or f"replan step {i+1}",
                                    status="pending",
                                )
                                st.actions.append(act)
                            except Exception:
                                pass
            except Exception:
                pass

    # Run the unified engine
    result = await run_diagnosis(
        state=state,
        tools=tools,
        prompt_builder=prompt_builder,
        memory=memory,
        event_emitter=emitter,
        llm_plan=_llm_plan,
        llm_call=llm_chat_fn,
        on_iteration=_on_iteration,
        logger=logger,
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
