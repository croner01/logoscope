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

    SYSTEM_TEMPLATE = """You are a senior SRE diagnosing issues in a Kubernetes-based observability platform (Logoscope).

## Known Target (do NOT rediscover)
The log entry under investigation already identifies the source:
{known_target}

**Critical rules about the known target:**
- The pod name, namespace, node, and labels above are FACTS from the log metadata. Do NOT run `kubectl get pods -A` or `kubectl get pods -l ...` to "discover" or "find" the pod — you already know it.
- Do NOT use `kubectl logs <pod>` — logs are queried through ClickHouse (see below), not via kubectl.

## How to Query Logs (ALWAYS use this — never kubectl logs)
Use the **clickhouse_query** tool to search logs directly in ClickHouse:
- Filter by the known `service_name`, `pod_name`, `namespace`, or `trace_id` above.
- Example: `SELECT * FROM logs.events WHERE pod_name='<known_pod>' AND level='ERROR' ORDER BY timestamp DESC LIMIT 50`
- This queries ALL logs from the service, not just the single log entry.
- Simple SELECT queries on logs.events are executed locally via query-service (fast).

## How to Check System State (ONLY when log evidence is insufficient)
Use the **generic_exec** tool for commands that MUST run on the target pod/node:
- Pod-level: `kubectl exec <known_pod> -n <known_ns> -- cat /etc/config.yaml`
- Pod-level: `kubectl exec <known_pod> -n <known_ns> -- ps aux`
- Pod-level: `kubectl exec <known_pod> -n <known_ns> -- df -h`
- Node-level: `kubectl describe node <known_node>`
- Always use the exact pod/namespace/node from the known target above.
- These commands are executed remotely via exec-service.

## Available Tools
{tool_schema}

## Previous Diagnostic Commands
{journal_context}

## Rules
1. Only propose read-only diagnostic commands.
2. Use the known target metadata — pod, namespace, node, labels, service_name are already known.
3. Start with clickhouse_query to explore logs; only escalate to generic_exec if logs are insufficient.
4. NEVER run `kubectl get pods -A` or `kubectl get pods -l` — you already know the pod.
5. NEVER use `kubectl logs` — query logs through ClickHouse instead.
6. Check the journal above — do not repeat commands already executed.
7. Stop and summarize when evidence is sufficient.
"""

    TASK_TEMPLATE = """## Question
{question}

## Known Source Metadata
{source_metadata}

## Analysis Context
{context}

## Observations So Far
{observations}
{replan_hint}

Plan the next diagnostic action. Output a tool call with command_spec."""

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
            "type": "function",
            "function": {
                "name": "execute_diagnostic_command",
                "description": (
                    "Execute a read-only diagnostic action. "
                    "Use clickhouse_query to search logs via ClickHouse (local, fast). "
                    "Use generic_exec ONLY for system state checks that cannot be done via logs "
                    "(ps, df, cat config, ss, systemctl status)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "enum": ["generic_exec", "clickhouse_query"],
                            "description": (
                                "clickhouse_query: search logs in ClickHouse (use this FIRST — "
                                "filter by known service_name/pod_name/namespace/trace_id). "
                                "generic_exec: run shell command on target pod/node (use ONLY when "
                                "logs are insufficient — ps, df, cat, ss, systemctl, kubectl describe)."
                            ),
                        },
                        "command": {
                            "type": "string",
                            "description": (
                                "For clickhouse_query: a SELECT SQL query on logs.events. "
                                "For generic_exec: a shell command targeting the known pod/node. "
                                "NEVER use 'kubectl get pods -A' or 'kubectl logs'."
                            ),
                        },
                        "target_kind": {
                            "type": "string",
                            "description": "k8s_cluster for shell commands, clickhouse_cluster for SQL",
                        },
                        "target_identity": {
                            "type": "string",
                            "description": (
                                "For k8s: pod:<name>/namespace:<ns> (use the known pod name). "
                                "For ClickHouse: database:logs"
                            ),
                        },
                        "purpose": {
                            "type": "string",
                            "description": "Why this command is needed and what evidence it will provide",
                        },
                    },
                    "required": ["tool", "command", "purpose"],
                },
            },
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
