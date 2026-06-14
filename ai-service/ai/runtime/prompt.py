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
You MUST output a JSON object wrapped in a ```json code fence. The system will EXECUTE your commands and return results. You will then see the results and can plan the next step.

## JSON SYNTAX RULES (violations will cause your output to be rejected)
- All keys and string values MUST use double quotes: "key": "value"
- NO trailing commas: the last item in an array or object has NO comma after it
- NO JavaScript comments (// or /* */)
- Output MUST start with ```json on its own line and end with ``` on its own line
- Inside the fence, the first character MUST be `{` and the last MUST be `}`

**Correct output (do this):**
```json
{"actions":[{"tool":"clickhouse_query","command":"SELECT service_name, pod_name, namespace, level, message FROM logs.logs WHERE pod_name='thanos-ruler-ecms' AND level='ERROR' ORDER BY timestamp DESC LIMIT 20","target_kind":"clickhouse_cluster","target_identity":"database:logs","purpose":"check error patterns around the YAML failure"}]}
```

**WRONG output (NEVER do this):**
- "Based on the log, the YAML syntax error at line 154..."
- "The root cause appears to be..."
- Any text before or after the ```json fence
- JSON objects without "command" or "tool" fields
- Using single quotes: {'tool':'clickhouse_query'}  ← REJECTED
- Trailing comma: {"actions":[{...},]}  ← REJECTED
- Querying logs.events — use logs.logs for log data (different schema)

## ClickHouse Schema

Primary table: **logs.logs**
Columns: timestamp, service_name, pod_name, namespace, node_name,
         container_name, container_image, host_ip, host_name,
         level, message, trace_id, span_id, labels, attributes_json

(Do NOT use logs.events — it has a different schema without pod/namespace columns.)

{known_target}

**Core rules:**
- The pod/namespace above are FACTS. Do NOT rediscover them.
- Do NOT use `kubectl get pods -A` — you already know the target.
- ALWAYS query logs.logs (NOT logs.events) for log entries.

## Decision Flow
1. **Check existing context first.** The "Context" section below already contains related logs, trace data, and metadata. If the question can be answered from this data, output your analysis WITHOUT querying ClickHouse or running commands.
2. **Query ClickHouse (clickhouse_query) PREFERRED for logs** — structured, filterable. BUT if the ClickHouse query returns `permission_required`, fall back to `kubectl logs <pod> -n <ns> --tail=N`.
3. **Search the web (web_search) when you need external knowledge** — e.g. looking up unknown error messages, finding known issues or solutions for specific software versions, searching for similar cases. Use error message keywords or "software version + symptom" as the search query.
4. **Run pod commands (generic_exec) ONLY when you need filesystem evidence** that logs cannot provide — e.g. checking config files, process state, disk usage.

## Tools
{tool_schema}

## Previously Executed Commands (DO NOT repeat)
{journal_context}

## Rules
1. Output ONLY a JSON object with an "actions" array. No analysis text.
2. Each action MUST have: tool, command, purpose. Include target_kind and target_identity.
3. For diagnostic questions (e.g. status, errors, recent behavior, root cause), ALWAYS generate at least one query to verify against live data. Empty actions are only valid for purely factual questions (e.g. "what is the schema?").
4. Tool priority: clickhouse_query (logs) → generic_exec (kubectl exec/ls/cat) → web_search.
5. **clickhouse_query fallback**: If the same session's previous ClickHouse query was denied (`permission_required`), skip ClickHouse and use `generic_exec` with `kubectl logs` instead.
6. NEVER run kubectl get pods -A, kubectl get pods -l.
7. Check the journal above — do not repeat already-executed commands.
8. If the question is about config/setup (not runtime errors), use generic_exec to check the config file.
9. Each command MUST be a single executable (kubectl, ls, cat, etc.) with flags/args. NEVER use shell programming constructs (for, while, until, if, case, function) — the sandbox only allows specific command heads. A "for f in ..." loop will be REJECTED. Use "ls ..." or "cat ..." directly.
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
{permission_hint}

**For diagnostic questions, always generate at least one query. Empty actions are only for factual lookups.**"""

    REPLAN_HINT = """## ⚠️ Replan Required
Previous actions did not resolve all evidence gaps. Review the observations above
and propose NEW diagnostic actions targeting the remaining unknowns.
Do NOT repeat commands that have already been executed."""

    # ── public API ──────────────────────────────────────────────────────────

    def build_system(self, state: RuntimeState, memory: SessionMemory) -> str:
        return (
            self.SYSTEM_TEMPLATE
            .replace("{known_target}", self._build_known_target(state))
            .replace("{tool_schema}", str(self.build_tool_schema()))
            .replace("{journal_context}", memory.context_for_llm() or "(no commands executed yet)")
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

        # Detect if a ClickHouse query was blocked by OPA → suggest kubectl logs fallback
        permission_hint = ""
        for obs in state.observations:
            obs_status = str(getattr(obs, "status", "") or "").strip().lower()
            obs_stderr = str(getattr(obs, "stderr", "") or "")
            if obs_status == "permission_required" or "policy denied by opa" in obs_stderr:
                permission_hint = (
                    "\n## ⚠️ ClickHouse Query Blocked\n"
                    "A previous ClickHouse query was denied by policy. "
                    "Query logs directly from the pod using `kubectl logs <pod> -n <ns> --tail=200` "
                    "instead. Do NOT retry the same ClickHouse query."
                )
                break

        return (
            self.TASK_TEMPLATE
            .replace("{question}", state.question)
            .replace("{source_metadata}", self._build_source_metadata(state))
            .replace("{context}", str(state.analysis_context)[:2000])
            .replace("{observations}", observations_text)
            .replace("{replan_hint}", replan_hint)
            .replace("{permission_hint}", permission_hint)
        )

    def build_tool_schema(self) -> Dict[str, Any]:
        return {
            "type": "json_schema",
            "name": "diagnostic_actions",
            "description": (
                "A JSON object with an 'actions' array of diagnostic commands. "
                "Use clickhouse_query to search logs. Use generic_exec for pod-level "
                "system checks. Use web_search for known errors, solutions, or case studies."
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
                                    "enum": ["clickhouse_query", "generic_exec", "web_search"],
                                    "description": "clickhouse_query=search logs in ClickHouse, generic_exec=run shell command on pod, web_search=search internet for solutions/cases"
                                },
                                "command": {
                                    "type": "string",
                                    "description": "SQL query (for clickhouse_query), shell command (for generic_exec), or search keywords (for web_search)"
                                },
                                "target_kind": {
                                    "type": "string",
                                    "description": "clickhouse_cluster or k8s_cluster (omit for web_search)"
                                },
                                "target_identity": {
                                    "type": "string",
                                    "description": "database:logs or pod:<name>/namespace:<ns> (omit for web_search)"
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
