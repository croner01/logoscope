"""CommandSpec → executable shell command.

Compiles a validated CommandSpec into a CompiledCommand ready for execution.
All commands (including ClickHouse queries) route to exec-service (remote);
the toolbox-gateway sandbox provides clickhouse-client for SQL execution.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ai.command.spec import CommandSpec, CompiledCommand, ToolType


_BLOCKED_OPERATORS = {";", "&", ">", ">>", "<", "<<", "||"}
# | and && are allowed in commands (piping and conditional chaining)

# Commands that need to run inside the target pod → auto-wrap with kubectl exec
_POD_INTERNAL_COMMANDS = {
    "ls", "cat", "ps", "df", "ss", "curl", "head", "tail",
    "grep", "rg", "awk", "jq", "sed", "echo", "pwd", "find",
    "stat", "du", "wc", "sort", "uniq", "xargs", "env", "id",
    "whoami", "hostname", "ip", "netstat", "lsof", "top", "free",
    "uptime", "uname", "pgrep", "pidof",
}

# Commands that run on the host node → route to SSH gateway
_HOST_COMMANDS = {
    "systemctl", "service", "journalctl", "dmesg",
    "hostnamectl", "timedatectl",
}

# Commands that are already kubectl → pass through to toolbox-gateway
_K8S_COMMANDS = {"kubectl", "helm"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _extract_head(command: str) -> str:
    """Extract the command head (first token)."""
    parts = command.strip().split()
    if not parts:
        return ""
    head = parts[0]
    if "/" in head:
        head = head.rsplit("/", 1)[-1]
    return head.lower()


def _parse_target_identity(target_identity: str) -> tuple[str, str]:
    """Parse 'pod:<name>/namespace:<ns>' into (pod, namespace)."""
    pod = ""
    ns = ""
    if not target_identity:
        return pod, ns
    for part in target_identity.split("/"):
        part = part.strip()
        if part.startswith("pod:"):
            pod = part[4:]
        elif part.startswith("namespace:"):
            ns = part[10:]
    return pod, ns


def _normalize_clickhouse_query(query: str) -> str:
    """Normalize a ClickHouse SQL query for robust matching.

    Fixes common LLM-generated mismatches against the actual schema:
      - level = 'VALUE' → lower(level) = lower('VALUE')
        (ClickHouse string comparison is case-sensitive; the logs table
        stores level as lowercase 'error'/'info', but LLMs often emit
        'ERROR'/'INFO'.)
    """
    # level = 'value' / level = "value" → lower(level) = lower(...)
    query = re.sub(
        r"(?i)\blevel\s*=\s*('[^']*'|\"[^\"]*\")",
        r"lower(level) = lower(\1)",
        query,
    )
    # level IN ('A', 'B') → lower(level) IN (lower('A'), lower('B'))
    def _wrap_in_values(m: re.Match) -> str:
        values = m.group(1)
        wrapped = re.sub(r"'[^']*'", lambda vm: f"lower({vm.group(0)})", values)
        return f"lower(level) IN ({wrapped})"

    query = re.sub(
        r"(?i)\blevel\s+IN\s*\(([^)]*)\)",
        _wrap_in_values,
        query,
    )
    return query


def _ensure_clickhouse_limit(query: str, default_limit: int = 1000) -> str:
    """Auto-append LIMIT {default_limit} to SELECT queries without one."""
    stripped = query.strip()
    upper = stripped.upper()
    if not upper.startswith("SELECT"):
        return stripped
    if re.search(r"\bLIMIT\s+\d", upper):
        return stripped
    if re.search(r"\bGROUP\s+BY\b", upper):
        return stripped
    cleaned = stripped.rstrip().rstrip(";")
    return f"{cleaned} LIMIT {default_limit}"


def _escape_clickhouse_query(query: str) -> str:
    """Escape a ClickHouse SQL query for use as a --query argument.

    Uses double quotes to avoid single-quote escaping conflicts when
    exec-service applies shlex.quote() to the outer kubectl exec command.
    Only double quotes and backslashes in the SQL need escaping.
    """
    return query.replace("\\", "\\\\").replace('"', '\\"')


def _resolve_clickhouse_target(namespace: str) -> str:
    """Resolve the ClickHouse kubectl exec target (deployment or pod).

    Returns e.g. 'deploy/clickhouse -n islap' or 'pod/clickhouse-0 -n islap'.
    Configurable via AI_RUNTIME_CLICKHOUSE_EXEC_TARGET env var.
    """
    target = os.getenv("AI_RUNTIME_CLICKHOUSE_EXEC_TARGET", "deploy/clickhouse")
    ns = os.getenv("AI_RUNTIME_CLICKHOUSE_EXEC_NAMESPACE", namespace)
    return f"{target} -n {ns}"


_KUBECTL_COMMON_VERBS = {
    "get", "describe", "logs", "exec", "top", "rollout", "apply", "delete",
    "patch", "edit", "replace", "scale", "set", "annotate", "label",
    "create", "expose", "autoscale", "cordon", "uncordon", "drain", "taint",
}


def _repair_kubectl_verb_value_glue(command: str) -> str:
    """Repair LLM-generated commands where a kubectl verb is concatenated
    with its resource value (e.g. 'execthanos-ruler-ecms-0-nopenstack'
    → 'exec thanos-ruler-ecms-0 -n openstack').

    Works on raw command text level, inserting missing spaces between
    a known kubectl verb and the resource identifier that follows, and
    splitting -n<namespace> glue when -n is preceded by a non-letter.
    """
    lowered = command.lower()
    # Pass 1: split verb-value concatenation
    for verb in _KUBECTL_COMMON_VERBS:
        pattern = re.compile(rf"(?i)(?<=\s){verb}(?=[a-z][-a-z0-9])")
        m = pattern.search(lowered)
        if m:
            start = m.start()
            end = m.end()
            before = command[:start]
            verb_text = command[start:end]
            after = command[end:]
            command = f"{before}{verb_text} {after}"
            lowered = command.lower()
            continue
    # Pass 2: split -n<namespace> when preceded by non-letter
    # e.g. "ecms-0-nopenstack" → "ecms-0 -n openstack"
    # but NOT "pod-nginx" (preceded by letter)
    command = re.sub(
        r'(?i)(?<=[^a-zA-Z])-n([a-z][-a-z0-9.]*)(?=\s|$)',
        r' -n \1',
        command,
    )
    # Clean up double spaces
    command = re.sub(r' {2,}', ' ', command).strip()
    return command


def compile_command(
    spec: CommandSpec,
    *,
    run_sql_preflight: bool = False,
    namespace: str = "islap",
) -> CompiledCommand:
    """Compile a CommandSpec into an executable command.

    Args:
        spec: Validated CommandSpec.
        run_sql_preflight: If True, run EXPLAIN SYNTAX on ClickHouse queries.
        namespace: Kubernetes namespace for ClickHouse pod resolution.

    Returns:
        CompiledCommand with route and executor_profile set.
    """
    command = _as_str(spec.command).strip()
    if not command:
        return CompiledCommand(spec=spec, shell_command="", route="")

    if spec.tool == ToolType.GENERIC_EXEC:
        # Blocked operator check (before wrapping).  | and && are allowed.
        # Exact-token match for standalone operators; ; is also checked
        # as substring since it often appears attached (cmd1;cmd2).
        tokens = command.split()
        for token in tokens:
            if token in _BLOCKED_OPERATORS or ";" in token:
                return CompiledCommand(spec=spec, shell_command="", route="")

        head = _extract_head(command)
        pod, ns = _parse_target_identity(spec.target_identity)
        ns = ns or namespace

        # ── Host-level commands → SSH gateway ──────────────────────────
        if head in _HOST_COMMANDS:
            return CompiledCommand(
                spec=spec,
                shell_command=command,
                route="remote",
                executor_profile="host-ssh-readonly",
            )

        # ── K8s commands → toolbox-gateway ─────────────────────────────
        if head in _K8S_COMMANDS:
            # Repair verb-value concatenation: "kubectl execthanos-ruler-ecms-0"
            # → "kubectl exec thanos-ruler-ecms-0" — handles LLM output where
            # a kubectl verb is glued to its resource value (common in NL->cmd extraction).
            repaired = _repair_kubectl_verb_value_glue(command)
            if repaired != command:
                command = repaired
                # Re-extract head in case repair changed it (it shouldn't for kubectl)
                head = _extract_head(command)
            return CompiledCommand(
                spec=spec,
                shell_command=command,
                route="remote",
                executor_profile="toolbox-k8s-readonly",
            )

        # ── Pod internal commands → auto-wrap with kubectl exec ────────
        if head in _POD_INTERNAL_COMMANDS:
            if pod:
                ns_flag = f"-n {ns}" if ns else ""
                wrapped = f"kubectl exec {pod} {ns_flag} -- {command}"
                return CompiledCommand(
                    spec=spec,
                    shell_command=wrapped,
                    route="remote",
                    executor_profile="toolbox-k8s-readonly",
                )
            else:
                # No target pod known — run in sandbox (limited use)
                return CompiledCommand(
                    spec=spec,
                    shell_command=command,
                    route="remote",
                    executor_profile="busybox-readonly",
                )

        # ── Unknown commands → best-effort kubectl exec if pod known ───
        if pod:
            ns_flag = f"-n {ns}" if ns else ""
            wrapped = f"kubectl exec {pod} {ns_flag} -- {command}"
            return CompiledCommand(
                spec=spec,
                shell_command=wrapped,
                route="remote",
                executor_profile="toolbox-k8s-readonly",
            )

        return CompiledCommand(
            spec=spec,
            shell_command=command,
            route="remote",
            executor_profile="toolbox-k8s-readonly",
        )

    if spec.tool == ToolType.WEB_SEARCH:
        # Web search doesn't produce a shell command — the engine calls
        # ToolAdapter.web_search() directly with the search query text.
        return CompiledCommand(
            spec=spec,
            shell_command=command,  # the search query
            route="web",
            executor_profile="web-search",
        )

    if spec.tool == ToolType.CLICKHOUSE_QUERY:
        # Wrap ClickHouse queries with kubectl exec so exec-service classifies
        # them as kubectl exec → toolbox-k8s-readonly → confirmation_required
        # (bypassable with a ticket) rather than permission_required (hard deny).
        # The toolbox-gateway sandbox has both kubectl and clickhouse-client.
        normalized = _normalize_clickhouse_query(command)
        limited = _ensure_clickhouse_limit(normalized)
        escaped = _escape_clickhouse_query(limited)
        target = _resolve_clickhouse_target(namespace)
        shell = f'kubectl exec {target} -- clickhouse-client --query "{escaped}"'
        return CompiledCommand(
            spec=spec,
            shell_command=shell,
            route="remote",
            executor_profile="toolbox-k8s-readonly",
            sql_preflight_passed=not run_sql_preflight,
        )

    return CompiledCommand(spec=spec, shell_command="", route="")


__all__ = ["compile_command"]
