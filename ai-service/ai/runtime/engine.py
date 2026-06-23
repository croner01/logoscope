"""Unified diagnosis engine — single plan→act→observe→replan loop.

All three entry points (v1 followup, v2 agent run, v4 LangGraph/Temporal)
call this one function.

Fixes audit H4: iteration counter properly advances every round.
Fixes audit H5: approval always goes through EventEmitter.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from ai.command.compiler import compile_command
from ai.command.normalizer import normalize_command_spec
from ai.command.security import evaluate_command
from ai.command.spec import CommandSpec, ToolType

from ai.runtime.state import RuntimeState, Action, Observation
from ai.runtime.memory import SessionMemory
from ai.runtime.events import EventEmitter
from ai.runtime.prompt import PromptBuilder
from ai.runtime.tools import ToolAdapter

logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _build_fallback_actions(source_target: Optional[dict], question: str = "") -> List[dict]:
    """当 LLM 未生成动作时，基于已知 target 生成默认诊断查询。"""
    st = source_target if isinstance(source_target, dict) else {}
    pod = _as_str(st.get("pod_name"))
    svc = _as_str(st.get("service_name"))
    ns = _as_str(st.get("namespace"))

    if not (pod or svc):
        return []

    filters = []
    if pod:
        filters.append(f"pod_name='{pod}'")
    if svc:
        filters.append(f"service_name='{svc}'")
    if ns:
        filters.append(f"namespace='{ns}'")
    where = " AND ".join(filters) if filters else "1=1"

    return [{
        "tool": "clickhouse_query",
        "command": (
            f"SELECT timestamp, service_name, pod_name, namespace, level, message "
            f"FROM logs.logs WHERE {where} AND level IN ('ERROR', 'FATAL') "
            f"ORDER BY timestamp DESC LIMIT 20"
        ),
        "target_kind": "clickhouse_cluster",
        "target_identity": "database:logs",
        "purpose": f"fallback: query recent errors for {svc or pod or ns or 'unknown target'}",
    }]


# ── LLM Client protocol ────────────────────────────────────────────────────

@dataclass
class LlmPlanResult:
    """Result of an LLM planning call."""
    actions: List[dict] = field(default_factory=list)
    summary: str = ""
    raw_response: str = ""


LlmPlanFn = Callable[..., Any]
"""Async callable: (system_prompt, task_prompt, tool_schema, state, memory) → LlmPlanResult"""


async def _default_llm_plan(
    system_prompt: str,
    task_prompt: str,
    tool_schema: dict,
    state: RuntimeState,
    memory: SessionMemory,
    llm_call: Any = None,
) -> LlmPlanResult:
    """Default LLM planning implementation.

    Calls the provided llm_call async function and parses the result
    into action dicts compatible with normalize_command_spec().
    """
    if llm_call is None:
        return LlmPlanResult(actions=[], summary="no LLM client configured")

    try:
        raw = await llm_call(system_prompt, task_prompt, tool_schema)
    except Exception as exc:
        return LlmPlanResult(actions=[], summary=f"LLM call failed: {exc}")

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return LlmPlanResult(actions=[], summary=f"LLM returned non-JSON: {raw[:200]}", raw_response=raw)

    if isinstance(raw, dict):
        # Extract {"actions": [...]} format that the PromptBuilder teaches the LLM
        if "actions" in raw and isinstance(raw["actions"], list):
            raw = raw["actions"]
        else:
            raw = [raw]

    actions = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            # Normalize common LLM output shapes
            cmd = item.get("command_spec") or item.get("command") or item
            if isinstance(cmd, dict):
                actions.append(cmd)
            elif isinstance(item, dict) and item.get("command"):
                actions.append(item)

    return LlmPlanResult(actions=actions, summary="", raw_response=str(raw)[:2000])


async def _stream_llm_plan(
    system_prompt: str,
    task_prompt: str,
    tool_schema: dict,
    state: RuntimeState,
    memory: SessionMemory,
    llm_call: Any = None,
    event_emitter: Optional[EventEmitter] = None,
) -> LlmPlanResult:
    """Streaming LLM planning — emit token events while collecting full result.

    llm_call must be an async generator function (returns AsyncIterator[str])
    that yields text chunks.  This is the streaming counterpart of
    _default_llm_plan.
    """
    if llm_call is None:
        return LlmPlanResult(actions=[], summary="no LLM client configured")

    collected = ""
    try:
        async for chunk in llm_call(system_prompt, task_prompt, tool_schema):
            collected += chunk
            if event_emitter and state.run_id:
                await event_emitter.emit(state.run_id, "assistant_delta", {"text": chunk})
    except (TimeoutError, ConnectionError) as exc:
        logger.warning("_stream_llm_plan: LLM stream interrupted: %s", exc)
        return LlmPlanResult(actions=[], summary=f"LLM stream interrupted: {exc}", raw_response=collected[:2000])
    except Exception as exc:
        return LlmPlanResult(actions=[], summary=f"LLM call failed: {exc}")

    if not collected.strip():
        return LlmPlanResult(actions=[], summary="LLM returned empty response")

    # Same JSON-parsing logic as _default_llm_plan above
    try:
        raw = json.loads(collected)
    except json.JSONDecodeError:
        return LlmPlanResult(
            actions=[], summary=f"LLM returned non-JSON", raw_response=collected[:2000],
        )

    if isinstance(raw, dict):
        # Extract {"actions": [...]} format that the PromptBuilder teaches the LLM
        if "actions" in raw and isinstance(raw["actions"], list):
            raw = raw["actions"]
        else:
            raw = [raw]

    actions = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            cmd = item.get("command_spec") or item.get("command") or item
            if isinstance(cmd, dict):
                actions.append(cmd)
            elif isinstance(item, dict) and item.get("command"):
                actions.append(item)

    return LlmPlanResult(actions=actions, summary="", raw_response=collected[:2000])


# ── Runtime Result ─────────────────────────────────────────────────────────

@dataclass
class RuntimeResult:
    summary: str = ""
    observations: List[Observation] = field(default_factory=list)
    memory_snapshot: list = field(default_factory=list)
    react_loop: dict = field(default_factory=dict)
    actions: List[dict] = field(default_factory=list)


# ── Main engine ────────────────────────────────────────────────────────────

async def run_diagnosis(
    state: RuntimeState,
    *,
    tools: ToolAdapter,
    prompt_builder: PromptBuilder,
    memory: SessionMemory,
    event_emitter: EventEmitter,
    llm_plan: LlmPlanFn | None = None,
    llm_call: Any = None,
    on_iteration: Callable | None = None,
    logger: Any = None,
) -> RuntimeResult:
    """Run the diagnosis loop.

    All three API entry points eventually call this function.

    Args:
        state: Runtime state with question, context, max_iterations, etc.
        tools: ToolAdapter for executing compiled commands.
        prompt_builder: PromptBuilder for assembling LLM prompts.
        memory: SessionMemory for dedup and result journaling.
        event_emitter: EventEmitter for SSE event fan-out.
        llm_plan: Optional custom LLM planning function.
        llm_call: Raw LLM call function (used by default llm_plan).
        on_iteration: Optional callback called after each iteration.
        logger: Optional logger for pipeline observability.
    """
    deadline = time.monotonic() + state.timeout_seconds
    plan_fn = llm_plan or _default_llm_plan
    react_iterations = []

    for iteration in range(1, state.max_iterations + 1):
        if time.monotonic() > deadline:
            state.diagnosis_summary = "诊断超时"
            break

        state.iteration = iteration

        # Collect replan actions added by on_iteration in the previous round
        # (bridge._on_iteration appends them to state.actions, which would be
        #  overwritten by the new LLM plan below)
        replan_actions = [a for a in state.actions if a.action_id.startswith("replan")]
        if replan_actions and logger:
            logger.info(
                "engine: session=%s iter=%d preserving %d replan actions from previous round",
                state.run_id, iteration, len(replan_actions),
            )

        # 1. PLAN — call LLM to generate actions
        system_prompt = prompt_builder.build_system(state, memory)
        task_prompt = prompt_builder.build_task(state)
        tool_schema = prompt_builder.build_tool_schema()

        plan = await plan_fn(system_prompt, task_prompt, tool_schema, state, memory, llm_call)

        if not plan.actions:
            # LLM 返回空 actions — 尝试基于已知 target 生成降级查询
            fallback = _build_fallback_actions(state.source_target, state.question)
            if fallback:
                plan = LlmPlanResult(
                    actions=fallback,
                    summary="LLM returned empty actions, using fallback query",
                )
                if logger:
                    logger.info(
                        "engine: session=%s iter=%d LLM returned empty, using fallback: %s",
                        state.run_id, iteration, fallback[0].get("command", "")[:80],
                    )
            else:
                # No target metadata available — genuinely done
                state.diagnosis_summary = plan.summary or "LLM 未生成诊断动作"
                break

        # Convert LLM output to Action objects
        iteration_actions = []
        for i, raw_action in enumerate(plan.actions):
            try:
                spec = normalize_command_spec(raw_action, source_target=state.source_target)
            except Exception as exc:
                if logger:
                    logger.warning(
                        "engine: session=%s iter=%d action[%d] normalize failed: %s raw_keys=%s",
                        state.run_id, iteration, i, exc,
                        list(raw_action.keys()) if isinstance(raw_action, dict) else type(raw_action).__name__,
                    )
                await event_emitter.emit(state.run_id, "tool_call_normalize_failed", {
                    "iteration": iteration,
                    "action_index": i,
                    "reason": str(exc)[:200],
                })
                continue

            action = Action(
                action_id=f"iter{iteration}-act{i}",
                command_spec=spec,
                purpose=spec.purpose or f"diagnostic step {i+1}",
                status="pending",
            )
            iteration_actions.append(action)

        # Prepend replan actions from the previous round so they execute first
        state.actions = replan_actions + iteration_actions
        if logger:
            logger.info(
                "engine: session=%s iter=%d plan=%d actions (llm=%d replan=%d)",
                state.run_id, iteration, len(state.actions), len(iteration_actions), len(replan_actions),
            )
        pending = [a for a in state.actions if a.status == "pending"]
        if not pending:
            break

        # 2. ACT — execute pending actions
        for action in pending:
            if time.monotonic() > deadline:
                break

            # Dedup check
            if memory.is_duplicate(action.command_spec):
                previously_blocked = memory.was_previously_blocked(action.command_spec)
                reason = (
                    "previously blocked by security — command contains disallowed operator"
                    if previously_blocked
                    else "previously executed in this session"
                )
                await event_emitter.emit(state.run_id, "tool_call_skipped_duplicate", {
                    "action_id": action.action_id,
                    "reason": reason,
                })
                action.status = "skipped_duplicate"
                if logger:
                    logger.info(
                        "engine: session=%s action=%s skipped duplicate (%s) cmd=%s",
                        state.run_id, action.action_id,
                        "was_blocked" if previously_blocked else "was_executed",
                        _as_str(action.command_spec.command)[:80],
                    )
                continue

            # Security
            decision = evaluate_command(action.command_spec, session_cost=state.cost)
            if not decision.allowed:
                if decision.requires_approval:
                    await event_emitter.emit(state.run_id, "approval_required", {
                        "action_id": action.action_id,
                        "command": action.command_spec.command,
                        "reason": decision.reason,
                    })
                    # In auto mode without UI, we skip approval-required commands
                    action.status = "blocked_approval"
                    memory.record_blocked(action.command_spec, f"approval required: {decision.reason}")
                    if logger:
                        logger.warning(
                            "engine: session=%s action=%s blocked_approval: %s",
                            state.run_id, action.action_id, decision.reason,
                        )
                    continue
                else:
                    action.status = "blocked"
                    memory.record_blocked(action.command_spec, decision.reason)
                    await event_emitter.emit(state.run_id, "tool_call_blocked", {
                        "action_id": action.action_id,
                        "command": action.command_spec.command,
                        "reason": decision.reason,
                    })
                    if logger:
                        logger.warning(
                            "engine: session=%s action=%s blocked: %s",
                            state.run_id, action.action_id, decision.reason,
                        )
                    continue

            # Web search — special path (no shell command, no exec-service)
            if action.command_spec.tool == ToolType.WEB_SEARCH:
                query = _as_str(action.command_spec.command).strip()
                if logger:
                    logger.info(
                        "engine: session=%s action=%s web_search query=%s",
                        state.run_id, action.action_id, query[:120],
                    )
                await event_emitter.emit(state.run_id, "tool_call_started", {
                    "action_id": action.action_id,
                    "command": f"web_search: {query[:100]}",
                    "route": "web",
                })
                result = await tools.web_search(query)
                await event_emitter.emit(state.run_id, "tool_call_finished", {
                    "action_id": action.action_id,
                    "command": action.command_spec.command,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:2000],
                    "duration_ms": result.duration_ms,
                })
                if logger:
                    logger.info(
                        "engine: session=%s action=%s web_search status=%s dur=%dms results=%s",
                        state.run_id, action.action_id,
                        result.status, result.duration_ms,
                        result.stdout[:120] if result.exit_code == 0 else result.stderr[:120],
                    )
                obs = Observation(
                    action_id=action.action_id,
                    status=result.status,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_ms=result.duration_ms,
                    channel=result.channel,
                    command=action.command_spec.command,
                )
                state.add_observation(action, obs)
                memory.record(
                    action.command_spec,
                    exit_code=result.exit_code,
                    summary=result.stdout[:120] if result.exit_code == 0 else f"failed: {result.stderr[:120]}",
                    output_preview=result.stdout[:2000],
                )
                state.cost.commands_executed += 1
                continue

            # Compile
            compiled = compile_command(action.command_spec)
            if not compiled.shell_command:
                action.status = "compile_failed"
                await event_emitter.emit(state.run_id, "tool_call_compile_failed", {
                    "action_id": action.action_id,
                    "command": action.command_spec.command,
                    "tool": str(action.command_spec.tool.value),
                })
                if logger:
                    logger.warning(
                        "engine: session=%s action=%s compile failed cmd=%s",
                        state.run_id, action.action_id,
                        _as_str(action.command_spec.command)[:80],
                    )
                continue

            if logger:
                logger.info(
                    "engine: session=%s action=%s executing route=%s profile=%s cmd=%s",
                    state.run_id, action.action_id,
                    compiled.route, compiled.executor_profile,
                    compiled.shell_command[:120],
                )

            # Execute
            await event_emitter.emit(state.run_id, "tool_call_started", {
                "action_id": action.action_id,
                "command": compiled.shell_command,
                "route": compiled.route,
            })

            result = await tools.execute(
                compiled,
                session_id=state.run_id,
                message_id=state.run_id,
                action_id=action.action_id,
            )

            await event_emitter.emit(state.run_id, "tool_call_finished", {
                "action_id": action.action_id,
                "command": compiled.shell_command,
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout[:10000],
                "stderr": result.stderr[:2000],
                "duration_ms": result.duration_ms,
            })

            if logger:
                logger.info(
                    "engine: session=%s action=%s executed status=%s exit=%d dur=%dms out_preview=%s",
                    state.run_id, action.action_id,
                    result.status, result.exit_code, result.duration_ms,
                    result.stdout[:120] if result.exit_code == 0 else result.stderr[:120],
                )

            # Record
            obs = Observation(
                action_id=action.action_id,
                status=result.status,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                channel=result.channel,
                command=compiled.shell_command,
            )
            state.add_observation(action, obs)
            memory.record(
                action.command_spec,
                exit_code=result.exit_code,
                summary=result.stdout[:120] if result.exit_code == 0 else f"failed: {result.stderr[:120]}",
                output_preview=result.stdout[:2000],
            )
            state.cost.commands_executed += 1

        # Track iteration
        react_iterations.append({
            "iteration": iteration,
            "actions": [a.action_id for a in iteration_actions],
            "observations": len(state.observations),
        })

        if on_iteration:
            try:
                await on_iteration(iteration, state, memory)
            except Exception:
                pass

        # 3. OBSERVE — sufficient evidence?
        if state.check_evidence_sufficient():
            state.phase = "done"
            break

        # 4. REPLAN — iteration counter properly advances (fix audit H4)

    if state.phase != "done":
        state.phase = "completed"

    return RuntimeResult(
        summary=state.build_summary(),
        observations=state.observations,
        memory_snapshot=memory.snapshot(),
        react_loop={
            "iterations": react_iterations,
            "phase": state.phase,
            "summary": state.diagnosis_summary or state.build_summary(),
        },
        actions=[{
            "id": a.action_id,
            "command": a.command_spec.command,
            "purpose": a.purpose,
            "status": a.status,
            "tool": a.command_spec.tool.value,
            "command_type": a.command_spec.command_type.value,
            "risk_level": a.command_spec.risk_level.value,
        } for a in state.actions],
    )


__all__ = ["run_diagnosis", "RuntimeResult", "LlmPlanResult", "LlmPlanFn", "_stream_llm_plan"]
