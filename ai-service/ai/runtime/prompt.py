"""Centralized prompt builder for the diagnosis engine."""
from __future__ import annotations

from typing import Any, Dict

from ai.runtime.state import RuntimeState
from ai.runtime.memory import SessionMemory


class PromptBuilder:
    """Builds all prompts for the diagnosis run.

    Centralizes what was previously scattered across:
    - followup_prompt_helpers.py
    - followup_planning_helpers.py
    - langgraph/nodes/planning.py
    - project_knowledge_pack.py
    """

    SYSTEM_TEMPLATE = """You are a senior SRE diagnosing issues in a Kubernetes-based observability platform.

Your task is to analyze the provided logs and context, then execute diagnostic commands
to identify the root cause.

## Available Tools
{tool_schema}

## Previous Diagnostic Commands
{journal_context}

## Rules
1. Only propose read-only diagnostic commands
2. Target specific pods/namespaces when known — do not use -A without good reason
3. Check the journal above — do not repeat commands already executed
4. Stop and summarize when evidence is sufficient
"""

    TASK_TEMPLATE = """## Question
{question}

## Context
{context}

## Observations So Far
{observations}
{replan_hint}

Plan the next diagnostic action. Output a tool call with command_spec."""

    REPLAN_HINT = """## ⚠️ Replan Required
Previous actions did not resolve all evidence gaps. Review the observations above
and propose NEW diagnostic actions targeting the remaining unknowns.
Do NOT repeat commands that have already been executed."""

    def build_system(self, state: RuntimeState, memory: SessionMemory) -> str:
        return self.SYSTEM_TEMPLATE.format(
            tool_schema=self.build_tool_schema(),
            journal_context=memory.context_for_llm() or "(no commands executed yet)",
        )

    def build_task(self, state: RuntimeState) -> str:
        obs_lines = []
        for obs in state.observations[-10:]:
            status = "✓" if obs.exit_code == 0 else "✗"
            obs_lines.append(
                f"  {status} [{obs.action_id}] exit={obs.exit_code} "
                f"stdout={obs.stdout[:200]} stderr={obs.stderr[:100]}"
            )
        observations_text = "\n".join(obs_lines) or "(none yet)"
        # Add replan hint when there are prior observations but evidence is still insufficient
        replan_hint = ""
        if state.observations and state.iteration >= 2:
            replan_hint = self.REPLAN_HINT
        return self.TASK_TEMPLATE.format(
            question=state.question,
            context=str(state.analysis_context)[:2000],
            observations=observations_text,
            replan_hint=replan_hint,
        )

    def build_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "execute_diagnostic_command",
                "description": "Execute a read-only diagnostic command on the target system",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "enum": ["generic_exec", "clickhouse_query"],
                            "description": "generic_exec for shell commands, clickhouse_query for SQL",
                        },
                        "command": {
                            "type": "string",
                            "description": "The shell command or SQL query to execute",
                        },
                        "target_kind": {
                            "type": "string",
                            "description": "k8s_cluster, clickhouse_cluster, or host_node",
                        },
                        "target_identity": {
                            "type": "string",
                            "description": "pod:<name>/namespace:<ns> or database:<name>",
                        },
                        "purpose": {
                            "type": "string",
                            "description": "One-line description of why this command is needed",
                        },
                    },
                    "required": ["tool", "command", "purpose"],
                },
            },
        }


__all__ = ["PromptBuilder"]
