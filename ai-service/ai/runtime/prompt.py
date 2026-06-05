"""Centralized prompt builder for the diagnosis engine.

All prompts include source_target metadata so the LLM knows exactly
which pod/namespace/node produced the log — no guessing needed.
"""
from __future__ import annotations

from typing import Any, Dict

from ai.runtime.state import RuntimeState
from ai.runtime.memory import SessionMemory


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class PromptBuilder:
    """Builds all prompts for the diagnosis run.

    Centralizes what was previously scattered across:
    - followup_prompt_helpers.py
    - followup_planning_helpers.py
    - langgraph/nodes/planning.py
    - project_knowledge_pack.py
    """

    SYSTEM_TEMPLATE = """You are a diagnostic execution engine. Your ONLY job is to output executable commands. Do NOT output analysis, conclusions, or recommendations — those come AFTER command results are observed.

## CRITICAL: Output Format — JSON ONLY, NO analysis text
You MUST output a JSON object containing diagnostic commands. The system will EXECUTE your commands and return results. You will then see the results and can plan the next step.

**Correct output (do this):**
```json
{"actions":[{"tool":"clickhouse_query","command":"SELECT * FROM logs.events WHERE pod_name='thanos-ruler-ecms' AND level='ERROR' LIMIT 20","target_kind":"clickhouse_cluster","target_identity":"database:logs","purpose":"check error patterns around the YAML failure"}]}
```

**WRONG output (NEVER do this):**
- "Based on the log, the YAML syntax error at line 154..."
- "The root cause appears to be..."
- Any text before or after the JSON
- JSON objects without "command" or "tool" fields

{known_target}

**Core rules:**
- The pod/namespace above are FACTS. Do NOT rediscover them.
- Do NOT use `kubectl logs` — query logs through ClickHouse.
- Do NOT use `kubectl get pods -A` — you already know the target.
- Start with clickhouse_query to explore logs. Only escalate to generic_exec if log evidence is insufficient.

## Tools
{tool_schema}

## Previously Executed Commands (DO NOT repeat)
{journal_context}

## Rules
1. Output ONLY a JSON object with an "actions" array. No analysis text.
2. Each action MUST have: tool, command, purpose. Include target_kind and target_identity.
3. clickhouse_query FIRST — search logs with known pod_name/namespace/service_name.
4. generic_exec ONLY when logs insufficient — use exact pod/namespace from known target.
5. NEVER run kubectl get pods -A, kubectl get pods -l, or kubectl logs.
6. Check the journal above — do not repeat already-executed commands.
7. If the question is about config/setup (not runtime errors), use generic_exec to check the config file.
"""

    TASK_TEMPLATE = """## Question
{question}

## Known Metadata
{source_metadata}

## Context
{context}

## Command Results So Far
{observations}
{replan_hint}

**Your response MUST be a JSON object with an "actions" array. Each action requires: tool, command, purpose. Include target_kind and target_identity. NO analysis text — only the JSON.**"""

    REPLAN_HINT = """## ⚠️ Replan Required
Previous actions did not resolve all evidence gaps. Review the observations above
and propose NEW diagnostic actions targeting the remaining unknowns.
Do NOT repeat commands that have already been executed."""

    # ── public API ──────────────────────────────────────────────────────────

    def build_system(self, state: RuntimeState, memory: SessionMemory) -> str:
        return self.SYSTEM_TEMPLATE.format(
            known_target=self._build_known_target(state),
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
        replan_hint = ""
        if state.observations and state.iteration >= 2:
            replan_hint = self.REPLAN_HINT
        return self.TASK_TEMPLATE.format(
            question=state.question,
            source_metadata=self._build_source_metadata(state),
            context=str(state.analysis_context)[:2000],
            observations=observations_text,
            replan_hint=replan_hint,
        )

    def build_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "json_schema",
            "name": "diagnostic_actions",
            "description": (
                "A JSON object with an 'actions' array of diagnostic commands. "
                "Use clickhouse_query FIRST to search logs. "
                "Use generic_exec ONLY for pod-level system checks (ps, df, cat, ss)."
            ),
            "schema": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "description": "Array of diagnostic commands to execute",
                        "items": {
                            "type": "object",
                            "properties": {
                                "tool": {
                                    "type": "string",
                                    "enum": ["clickhouse_query", "generic_exec"],
                                    "description": "clickhouse_query=search logs in ClickHouse, generic_exec=run shell command on pod"
                                },
                                "command": {
                                    "type": "string",
                                    "description": "SQL query (for clickhouse_query) or shell command (for generic_exec)"
                                },
                                "target_kind": {
                                    "type": "string",
                                    "description": "clickhouse_cluster or k8s_cluster"
                                },
                                "target_identity": {
                                    "type": "string",
                                    "description": "database:logs or pod:<name>/namespace:<ns>"
                                },
                                "purpose": {
                                    "type": "string",
                                    "description": "Why this command is needed"
                                }
                            },
                            "required": ["tool", "command", "purpose"]
                        }
                    }
                },
                "required": ["actions"]
            }
        }

    # ── internal helpers ────────────────────────────────────────────────────

    def _build_known_target(self, state: RuntimeState) -> str:
        """Build a compact description of the known source target."""
        st = state.source_target if isinstance(state.source_target, dict) else {}
        if not st:
            # Try to extract from analysis_context
            ctx = state.analysis_context if isinstance(state.analysis_context, dict) else {}
            service = _as_str(ctx.get("service_name") or ctx.get("source_service_name"))
            if service:
                return f"- service_name: {service}\n- (pod/namespace/node: not provided in this log entry)"
            return "- No known target metadata in this log entry. You may need to run kubectl get pods -l app=<service> to locate the pod."

        lines = []
        pod = _as_str(st.get("pod_name"))
        ns = _as_str(st.get("namespace"))
        node = _as_str(st.get("node_name"))
        host_ip = _as_str(st.get("host_ip"))
        container = _as_str(st.get("container_name"))
        svc = _as_str(st.get("service_name"))
        labels = st.get("labels")

        if svc:
            lines.append(f"- service_name: {svc}")
        if pod:
            lines.append(f"- pod_name: {pod}")
        if ns:
            lines.append(f"- namespace: {ns}")
        if node:
            lines.append(f"- node_name: {node}")
        if host_ip:
            lines.append(f"- host_ip: {host_ip}")
        if container:
            lines.append(f"- container_name: {container}")
        if isinstance(labels, dict) and labels:
            label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
            lines.append(f"- labels: {{{label_str}}}")

        return "\n".join(lines) if lines else "- No known target metadata."

    def _build_source_metadata(self, state: RuntimeState) -> str:
        """Brief metadata reminder for the task prompt."""
        st = state.source_target if isinstance(state.source_target, dict) else {}
        if not st:
            return "(no source metadata — use analysis context to identify the target)"
        pod = _as_str(st.get("pod_name"))
        ns = _as_str(st.get("namespace"))
        svc = _as_str(st.get("service_name"))
        parts = []
        if pod:
            parts.append(f"pod={pod}")
        if ns:
            parts.append(f"ns={ns}")
        if svc:
            parts.append(f"svc={svc}")
        return ", ".join(parts) if parts else "(partial metadata)"


__all__ = ["PromptBuilder"]
