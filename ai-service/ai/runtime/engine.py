"""Unified diagnosis engine — single plan→act→observe→replan loop.

All three entry points (v1 followup, v2 agent run, v4 LangGraph/Temporal)
call this one function.

Fixes audit H4: iteration counter properly advances every round.
Fixes audit H5: approval always goes through EventEmitter.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List

from ai.command.compiler import compile_command
from ai.command.normalizer import normalize_command_spec
from ai.command.security import evaluate_command

from ai.runtime.state import RuntimeState, Observation
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


@dataclass
class RuntimeResult:
    summary: str = ""
    observations: List[Observation] = field(default_factory=list)
    memory_snapshot: list = field(default_factory=list)


async def run_diagnosis(
    state: RuntimeState,
    *,
    tools: ToolAdapter,
    prompt_builder: PromptBuilder,
    memory: SessionMemory,
    event_emitter: EventEmitter,
) -> RuntimeResult:
    """Run the diagnosis loop.

    All three API entry points eventually call this function.
    """
    deadline = time.monotonic() + state.timeout_seconds

    for iteration in range(1, state.max_iterations + 1):
        # Timeout check (every iteration, fixes audit M6)
        if time.monotonic() > deadline:
            state.diagnosis_summary = "诊断超时"
            break

        state.iteration = iteration

        # 1. PLAN — prompt builder assembles context, LLM returns actions
        # (LLM call goes here — for now, caller pre-populates state.actions)
        prompt_builder.build_system(state, memory)
        prompt_builder.build_task(state)

        if not state.actions:
            break

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
                continue

            # Normalize
            try:
                spec = normalize_command_spec(
                    {"tool": str(action.command_spec.tool.value),
                     "command": action.command_spec.command,
                     "target_kind": action.command_spec.target_kind,
                     "target_identity": action.command_spec.target_identity,
                     "purpose": action.purpose},
                    source_target=state.source_target,
                )
            except Exception:
                continue

            # Security
            decision = evaluate_command(spec, session_cost=state.cost)
            if not decision.allowed:
                if decision.requires_approval:
                    approved = await event_emitter.request_approval(state.run_id, decision)
                    if not approved:
                        memory.record_blocked(spec, "approval denied")
                        continue
                else:
                    memory.record_blocked(spec, decision.reason)
                    continue

            # Compile
            compiled = compile_command(spec)
            if not compiled.shell_command:
                continue

            # Execute
            await event_emitter.emit(state.run_id, "tool_call_started", {
                "action_id": action.action_id,
                "command": compiled.shell_command,
            })

            result = await tools.execute(compiled)

            await event_emitter.emit(state.run_id, "tool_call_finished", {
                "action_id": action.action_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout[:2000],
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
                spec,
                exit_code=result.exit_code,
                summary=result.stdout[:120],
                output_preview=result.stdout[:2000],
            )
            state.cost.commands_executed += 1

        # 3. OBSERVE — sufficient evidence?
        if state.check_evidence_sufficient():
            state.phase = "done"
            break

        # 4. REPLAN — iteration counter properly advances (fix audit H4)
        state.actions = []

    if state.phase != "done":
        state.phase = "completed"

    return RuntimeResult(
        summary=state.build_summary(),
        observations=state.observations,
        memory_snapshot=memory.snapshot(),
    )


__all__ = ["run_diagnosis", "RuntimeResult"]
