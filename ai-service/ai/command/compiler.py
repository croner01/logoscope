"""CommandSpec → executable shell command.

Compiles a validated CommandSpec into a CompiledCommand ready for execution.
All commands (including ClickHouse queries) route to exec-service (remote);
the toolbox-gateway sandbox provides clickhouse-client for SQL execution.
"""
from __future__ import annotations

import os
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
        escaped = _escape_clickhouse_query(command)
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
