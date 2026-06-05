"""Unified diagnosis engine — single plan→act→observe→replan loop.

All three entry points (v1 followup, v2 agent run, v4 LangGraph/Temporal)
call this one function.

Fixes audit H4: iteration counter properly advances every round.
Fixes audit H5: approval always goes through EventEmitter.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ai.command.compiler import compile_command
from ai.command.normalizer import normalize_command_spec
from ai.command.security import evaluate_command
from ai.command.spec import CommandSpec, ToolType

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
    """
    deadline = time.monotonic() + state.timeout_seconds
    plan_fn = llm_plan or _default_llm_plan
    react_iterations = []

    for iteration in range(1, state.max_iterations + 1):
        if time.monotonic() > deadline:
            state.diagnosis_summary = "诊断超时"
            break

        state.iteration = iteration

        # 1. PLAN — call LLM to generate actions
        system_prompt = prompt_builder.build_system(state, memory)
        task_prompt = prompt_builder.build_task(state)
        tool_schema = prompt_builder.build_tool_schema()

        plan = await plan_fn(system_prompt, task_prompt, tool_schema, state, memory, llm_call)

        if not plan.actions:
            # No actions from LLM — we're done
            state.diagnosis_summary = plan.summary or "LLM 未生成诊断动作"
            break

        # Convert LLM output to Action objects
        iteration_actions = []
        for i, raw_action in enumerate(plan.actions):
            try:
                spec = normalize_command_spec(raw_action, source_target=state.source_target)
            except Exception:
                continue

            action = Action(
                action_id=f"iter{iteration}-act{i}",
                command_spec=spec,
                purpose=spec.purpose or f"diagnostic step {i+1}",
                status="pending",
            )
            iteration_actions.append(action)

        state.actions = iteration_actions
        pending = [a for a in state.actions if a.status == "pending"]
        if not pending:
            break

        # 2. ACT — execute pending actions
        for action in pending:
            if time.monotonic() > deadline:
                break

            # Dedup check
            if memory.is_duplicate(action.command_spec):
                await event_emitter.emit(state.run_id, "tool_call_skipped_duplicate", {
                    "action_id": action.action_id,
                    "reason": "previously executed in this session",
                })
                action.status = "skipped_duplicate"
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
                    continue
                else:
                    action.status = "blocked"
                    memory.record_blocked(action.command_spec, decision.reason)
                    continue

            # Compile
            compiled = compile_command(action.command_spec)
            if not compiled.shell_command:
                action.status = "compile_failed"
                continue

            # Execute
            await event_emitter.emit(state.run_id, "tool_call_started", {
                "action_id": action.action_id,
                "command": compiled.shell_command,
                "route": compiled.route,
            })

            result = await tools.execute(compiled)

            await event_emitter.emit(state.run_id, "tool_call_finished", {
                "action_id": action.action_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout[:2000],
                "duration_ms": result.duration_ms,
            })

            # Record
            obs = Observation(
                action_id=action.action_id,
                status=result.status,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                channel=result.channel,
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
        } for a in state.actions],
    )


__all__ = ["run_diagnosis", "RuntimeResult", "LlmPlanResult", "LlmPlanFn"]
